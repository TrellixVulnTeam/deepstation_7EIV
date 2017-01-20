import math
import numpy as np
import six
import chainer
from chainer import cuda
from chainer import functions as F
from chainer import links as L
from chainer import initializers
from chainer import function
from chainer import link
from chainer.utils import array
from chainer.utils import type_check

def crelu(x):
    h1 = F.relu(x)
    h2 = F.relu(-x)
    return F.concat((h1, h2), axis=1)

class BatchConv2D(chainer.Chain):
    def __init__(self, ch_in, ch_out, ksize, stride=1, pad=0, activation=F.relu):
        super(BatchConv2D, self).__init__(
            conv=L.Convolution2D(ch_in, ch_out, ksize, stride, pad),
            bn=L.BatchNormalization(ch_out),
        )
        self.activation=activation

    def __call__(self, x, train):
        h = self.bn(self.conv(x), test=not train)
        if self.activation is None:
            return h
        return self.activation(h)

class WeightNormalize(function.Function):

    """weight normalization"""

    def __init__(self, eps=1e-5):
        self.eps = eps

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() == 2)
        x_type, g_type = in_types

        type_check.expect(
            x_type.dtype == np.float32,
            x_type.dtype == np.float32,
            g_type.ndim == 0,
        )

    def forward(self, inputs):
        x, g = inputs
        xp = cuda.get_array_module(x)
        norm = xp.linalg.norm(x) + self.eps
        return g * x / norm,

    def backward(self, inputs, gy):
        x, g = inputs
        gy = gy[0]
        xp = cuda.get_array_module(x)

        norm = xp.linalg.norm(x) + self.eps
        gg = (x * gy).sum() / norm
        gx = g * (gy * norm - gg * x)
        gx = gx / norm ** 2

        return gx, gg


def weight_normalize(x, g, eps=1e-5):
    return WeightNormalize(eps)(x, g)


class WeightNormalization(link.Link):
    def __init__(self, func, W_shape, **kwargs):
        super(WeightNormalization, self).__init__()
        self.func = func
        self.kwargs = kwargs
        self.add_param('V', W_shape, initializer=chainer.initializers.Normal(0.05))
        self.add_uninitialized_param('g')
        self.add_uninitialized_param('b')

    def _initialize_params(self, t):
        xp = cuda.get_array_module(t.data)
        mean = xp.mean(t.data, axis=(0,) + tuple(six.moves.range(2, t.ndim)))
        std = xp.std(t.data)
        self.add_param('g', ())
        self.g.data[...] = 1 / std
        self.add_param('b', (t.shape[1],))
        self.b.data[...] = - mean / std

    def __call__(self, x):
        if self.has_uninitialized_params:
            xp = cuda.get_array_module(self.V.data)
            W = weight_normalize(self.V, xp.asarray(1, self.V.dtype))
            t = self.func(x, W, None, **self.kwargs)
            self._initialize_params(t)
        W = weight_normalize(self.V, self.g)
        return self.func(x, W, self.b, **self.kwargs)


class WNConv2D(chainer.Chain):
    def __init__(self, ch_in, ch_out, ksize, stride=1, pad=0, activation=F.relu):
        if hasattr(ksize, '__getitem__'):
            kh, kw = ksize
        else:
            kh, kw = ksize, ksize
        super(WNConv2D, self).__init__(
            wn_conv=WeightNormalization(F.convolution_2d, (ch_out, ch_in, kh, kw), stride=stride, pad=pad),
        )
        self.activation=activation

    def __call__(self, x):
        h = self.wn_conv(x)
        if self.activation is None:
            return h
        return self.activation(h)

class CReLUBlock(chainer.Chain):
    def __init__(self, ch_in, ch_out, ksize, stride=1, pad=0, activation=F.relu):
        super(CReLUBlock, self).__init__(
            conv=L.Convolution2D(ch_in, ch_out // 2, ksize, stride, pad),
            bn=L.BatchNormalization(ch_out),
        )
        self.activation=activation

    def __call__(self, x, train):
        h = self.conv(x)
        h = F.concat((h, -h), axis=1)
        h = self.bn(h, test=not train)
        if self.activation is None:
            return h
        return self.activation(h)

class ResidualBlock(chainer.Chain):
    def __init__(self, ch_in, ch_out, stride=1, swapout=False, skip_ratio=0, activation1=F.relu, activation2=F.relu):
        w = math.sqrt(2)
        super(ResidualBlock, self).__init__(
            conv1=L.Convolution2D(ch_in, ch_out, 3, stride, 1, w),
            bn1=L.BatchNormalization(ch_out),
            conv2=L.Convolution2D(ch_out, ch_out, 3, 1, 1, w),
            bn2=L.BatchNormalization(ch_out),
        )
        self.activation1 = activation1
        self.activation2 = activation2
        self.skip_ratio = skip_ratio
        self.swapout = swapout

    def __call__(self, x, train):
        skip = False
        if train and self.skip_ratio > 0 and np.random.rand() < self.skip_ratio:
            skip = True
        sh, sw = self.conv1.stride
        c_out, c_in, kh, kw = self.conv1.W.data.shape
        b, c, hh, ww = x.data.shape
        if sh == 1 and sw == 1:
            shape_out = (b, c_out, hh, ww)
        else:
            hh = (hh + 2 - kh) // sh + 1
            ww = (ww + 2 - kw) // sw + 1
            shape_out = (b, c_out, hh, ww)
        h = x
        if x.data.shape != shape_out:
            xp = chainer.cuda.get_array_module(x.data)
            n, c, hh, ww = x.data.shape
            pad_c = shape_out[1] - c
            p = xp.zeros((n, pad_c, hh, ww), dtype=xp.float32)
            p = chainer.Variable(p, volatile=not train)
            x = F.concat((p, x))
            if x.data.shape[2:] != shape_out[2:]:
                x = F.average_pooling_2d(x, 1, 2)
        if skip:
            return x
        h = self.bn1(self.conv1(h), test=not train)
        if self.activation1 is not None:
            h = self.activation1(h)
        h = self.bn2(self.conv2(h), test=not train)
        if not train:
            h = h * (1 - self.skip_ratio)
        if self.swapout:
            h = F.dropout(h, train=train) + F.dropout(x, train=train)
        else:
     
    def __init__(self, ch_in, ch_out, stride=1, swapout=False, skip_ratio=0, activation1=F.relu, activation2=F.relu):
        w = math.sqrt(2)
        super(IdentityMappingBlock, self).__init__(
            bn1=L.BatchNormalization(ch_in),
            conv1=L.Convolution2D(ch_in, ch_out, 3, stride, 1, w),
            conv2=L.Convolution2D(ch_out, ch_out, 3, 1, 1, w),
            bn2=L.BatchNormalization(ch_out),
        )
        self.activation1 = activation1
        w + 1
            shape_out = (b, c_out, hh, ww)
        h = x
        if x.data.shape != shape_out:
            xp = chainer.cudBlock(chainer.Chain):
    def __init__(self, ch_in, ch_out, stride=1, activation=F.relu, skip_ratio=0):
        initializer = initializers.Normal(scale=math.sqrt(2.0 / (ch_out * 3 * 3)))
        super(PyramidBlock, self).__init__(
            conv1=L.Convolution2D(ch_in, ch_out, 3, stride, 1, initialW=initialihh, ww)
        else:
            hh = (hh + 2 - kh) // sh + 1
            ww = (ww + 2 - kw) // sw + 1
            shape_out = (b, c_out, hh, ww)
        h = x
        if x.data.shape[2:] != shape_out[2:]:
            x = F.average_pooling_2d(x, 1, 2)
        if x.data.shape[1] != c_out:
            n, c, hh, ww = x.data.shape
            pad_c = c_out - c
            p = xp.zeros((n, pad_c, hh, ww), dtype=xp.float32)
            p = chainer.Variable(p, volatile=not train)
            x = F.concat((x, p), axis=1)
    olution2D(3, 64, 5, stride=1, pad=2),
            conv2=L.Convolution2D(64, 64, 5, stride=1, pad=2),
            conv3=L.Convolution2D(64, 128, 5, stride=1,
            pad=2),
            l1=L.Linear(4 * 4 * 128, 1000),
            l2=L.Linear(1000, 10),
        )

    def __call__(self, x, train=True):
        h1 = F.max_pooling_2d(F.relu(self.conv1(x)), 3, 2)
        h2 = F.max_pooling_2d(F.relu(self.conv2(h1)), 3, 2)
        h3 = F.max_pooling_2d(F.relu(self.conv3(h2)), lf.bconv1(x, train), 3, 2)
        h2 = F.max_pooling_2d(self.bconv2(h1, train), 3, 2)
        h3 = F.max_pooling_2d(self.bconv3(h2, train), 3, 2)
        h4 = F.relu(self.l1(F.dropout(h3, train=train)))
        return self.l2(F.dropout(h4, train=train))

class CNNWN(chainer.Chain):
    def __init__(self):
        super(CNNWN, self).__init__(
            wn_conv1=WNConv2D(3, 64, 5, stride=1, pad=2),
            wn_conv2=WNConv2D(64, 64, 5, stride=1, pad=2),
     nvconv3_3(h, train)
        h = self.bconv3_4(h, train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = F.relu(self.fc4(F.dropout(h, train=train)))
        h = F.relu(self.fc5(F.dropout(h, train=train)))
        h = self.fc6(h)
        return h

class VGGNoFC(chainer.Chain):
    def __init__(self):
        super(VGG, self).__init__(
            bconv1_1=BatchConv2D(3, 64, 3, stride=1, pad=1),
            bconv1_2=BatchConv2D(64, 64, 3, stride=1, pad=1),
            bconv2_1=BatchConv2D(64, 128, 3, stride=1, pad=1),
            bconv2_2=BatchConv2D(128, 128, 3, stride=1, pad=1),
            bconv3_1=BatchConv2D(128, 256,         h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = self.bconv2_1(h, train)
        h = self.bconv2_2(h, train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = self.bconv3_1(h, train)
        h = self.bconv3_2(h, train)
        h = self.bconv3_3(h, train)
        h = self.bconv3_4(h, train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = F.average_pooling_2d(h, 4, 1, 0)
     D(256, 256, 3, stride=1, pad=1),
            bconv2_4=BatchConv2D(256, 256, 3, stride=1, pad=1),
            bconv3_1=BatchConv2D(256, 512, 3, stride=1, pad=1),
            bconv3_2=BatchConv2D(512, 512, 3, stride=1, pad=1),
            bconv3_3=BatchConv2D(512, 512, 3, stride=1, pad=1),
            bconv3_4=BatchConv2D(512, 512, 3, stride=1, pad=1),
            bconv3_5=BatchConv2D(512, 512, 3, stride=1, pad=1),
           rain=train)
        h = self.bconv2_1(h, train)
        h = F.dropout(h, 0.25, train=train)
        h = self.bconv2_2(h, train)
        h = F.droh = F.average_pooling_2d(h, 4, 1, 0)
        h = self.fc(F.dropout(h, train=trai0], 1, stride=1, pad=0),
            conv6_2=BatchConv2D(out_ch[2][0], out_ch[2][1], 3, stride=1, pad=1),
            conv6_3, (32, (16, 32), (16, 32, 32), 64)),
            l1_2=InceptionBlock(64, (32, (16, 32), (16, 32, 32), 64)),
            l2_1=InceptionBlock(64, (64, (32, 64), (32, 64, 64), 128)),
            l2_2=InceptionBlock(128, (64, (32, 64), (32, 64, 64), 128)),
            l3_1=InceptionBlock(128, (128, (64, 128), (64, 128, 128), 256)),
            l3_2=InceptionBlock(256, (128, (64, 128), (64, 128, 128), 256)),
            l4_1=InceptionBlock(256, (128, (64, 128), (64, 128, 128), 256)),
            l4_2=InceptionBlock(256, (128, (64, 12, train=train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = self.l4_1(h, train=train)
        h = self.l4_2(h, train=train)
        h = F.dropout(h, 0.25, train=train)
        h = F.average_pooling_2d(h, 4, 1, 0)
        h = self.fc(h)
        return h

class VGGCReLU(chainer.Chain):
    def __init__(self):
        super(CReLUVGG, self).__init__(
            bconv1_1=CReLUBlock(3, 64, 3, stride=1, pad=1),
            bconv1_2=CReLUBlock(6_(self, x, train=True):
        h = self.bconv1_1(x, train)
        h = self.bconv1_2(h, train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = self.bconv2_1(h, train)
        h = self.bconv2_2(h, train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
        h = self.bconv3_1(h, train)
        h = self.bconv3_2(h, train)
        h = self.bconv3_3(h, train)
        h = self.bconv3_4(h, train)
        h = F.dropout(F.max_pooling_2d(h, 2), 0.25, train=train)
       else:
                skip_ratio = 0
            links.append(('res{}'.format(len(links)), ResidualBlock(16, 16, swapout=swapout, skip_ratio=skip_ratio, ), True))
  {}'.format(len(links)), F.AveragePooling2D(8, 1, 0, False, True), False))
        links.append(('fc{}'.format(len(links)), L.Linear(64, 10), False))

        for name, f, _with_train in links:
            if not name.startswith('_'):
             6, swapout=swapout, skip_ratio=skip_ratio, activation1=F.relu, activation2=F.relu), True))
        links.append(('res{}'.format(len(links)), IdentityMappingBlock(16, 32, stride=2, swapout=swapout, activation1=F.relu, activation2=F.relu), True))
        for i in six.moves.range(depth - 1):
            if skip:
                skip_ratio = float(i + depth) / skip_size * 0.5
          ut=swapout, skip_ratio=skip_ratio, activation1=F.relu, activation2=F.relu), True))
        links.append(('_apool{}'.format(len(links)), F.AveragePooling2D(8, 1, 0, False, True), False))
        links.append(('fc{}'.format(len(links)), L.Linear(64, 10), False))

        for name, f, _with_train in links:
            if not name.startswith('_'):
                self.add_link(*(n         if skip:
                skip_ratio = float(i) / skip_size * 0.5
            else:
                skip_ratio = 0
            in_channel = channel
            channel += channel_diff
            links.append(('py{}'.format(len(links)), PyramidBlock(int(round(in_channel)), int(round(channel)),  skip_ratio=skip_ratio), True, False))
     ormat(len(links)), PyramidBlock(int(round(in_channel)), int(round(channel)), stride=2), True, False))
        for i in six.moves.range(depth - 1):
            if skip:
                skip_ratio = float(i + depth * 2 - 1) / skip_size * 0.5
            else:
                skip_ratio = 0
            in_channel = channel
            channel += channel_diff
            links.appennk(*(name, f))
        self.layers = links

    def __call__(self, x, train=True):
        h = x
        for name, f, with_train, with_test in self.layers:
            if with_train:
                h = f(h, train=train)
            elif with_test:
                h = f(h, test=not train)
            else:
                h = f(h)
        return h
