# Copyright 2020 InterDigital Communications, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from compressai.entropy_models import (EntropyBottleneck, GaussianConditional)
from compressai.layers import GDN, MaskedConv2d
from compressai.ans import BufferedRansEncoder, RansDecoder  # pylint: disable=E0611,E0401

from .utils import update_registered_buffers, conv, deconv

__all__ = [
    'CompressionModel',
    'FactorizedPrior',
    'ScaleHyperprior',
    'MeanScaleHyperprior',
    'JointAutoregressiveHierarchicalPriors',
]


class CompressionModel(nn.Module):
    """Base class for constructing an auto-encoder with at least one entropy
    bottleneck module.

    Args:
        entropy_bottleneck_channels (int): Number of channels of the entropy
            bottleneck
    """
    def __init__(self, entropy_bottleneck_channels, init_weights=True):
        super().__init__()
        self.entropy_bottleneck = EntropyBottleneck(
            entropy_bottleneck_channels)

        if init_weights:
            self._initialize_weights()

    def aux_loss(self):
        """Return the aggregated loss over the auxiliary entropy bottleneck
        module(s).
        """
        aux_loss = sum(m.loss() for m in self.modules()
                       if isinstance(m, EntropyBottleneck))
        return aux_loss

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, (nn.Conv2d, nn.ConvTranspose2d)):
                nn.init.kaiming_normal_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def forward(self, *args):
        raise NotImplementedError()

    def parameters(self):
        """Returns an iterator over the model parameters."""
        for m in self.children():
            if isinstance(m, EntropyBottleneck):
                continue
            for p in m.parameters():
                yield p

    def aux_parameters(self):
        """
        Returns an iterator over the entropy bottleneck(s) parameters for
        the auxiliary loss.
        """
        for m in self.children():
            if not isinstance(m, EntropyBottleneck):
                continue
            for p in m.parameters():
                yield p

    def update(self, force=False):
        """Updates the entropy bottleneck(s) CDF values.

        Needs to be called once after training to be able to later perform the
        evaluation with an actual entropy coder.

        Args:
            force (bool): overwrite previous values (default: False)

        """
        for m in self.children():
            if not isinstance(m, EntropyBottleneck):
                continue
            m.update(force=force)


class FactorizedPrior(CompressionModel):
    r"""Factorized Prior model from J. Balle, D. Minnen, S. Singh, S.J. Hwang,
    N. Johnston: `"Variational Image Compression with a Scale Hyperprior"
    <https://arxiv.org/abs/1802.01436>`_, Int Conf. on Learning Representations
    (ICLR), 2018.

    Args:
        N (int): Number of channels
        M (int): Number of channels in the expansion layers (last layer of the
            encoder and last layer of the hyperprior decoder)
    """
    def __init__(self, N, M, **kwargs):
        super().__init__(entropy_bottleneck_channels=M, **kwargs)

        self.g_a = nn.Sequential(
            conv(3, N),
            GDN(N),
            conv(N, N),
            GDN(N),
            conv(N, N),
            GDN(N),
            conv(N, M),
        )

        self.g_s = nn.Sequential(
            deconv(M, N),
            GDN(N, inverse=True),
            deconv(N, N),
            GDN(N, inverse=True),
            deconv(N, N),
            GDN(N, inverse=True),
            deconv(N, 3),
        )

    def forward(self, x):
        y = self.g_a(x)
        y_hat, y_likelihoods = self.entropy_bottleneck(y)
        x_hat = self.g_s(y_hat)

        return {
            'x_hat': x_hat,
            'likelihoods': {
                'y': y_likelihoods,
            },
        }

    def load_state_dict(self, state_dict):
        # Dynamically update the entropy bottleneck buffers related to the CDFs
        update_registered_buffers(self.entropy_bottleneck,
                                  'entropy_bottleneck',
                                  ['_quantized_cdf', '_offset', '_cdf_length'],
                                  state_dict)
        super().load_state_dict(state_dict)

    @classmethod
    def from_state_dict(cls, state_dict):
        """Return a new model instance from `state_dict`."""
        N = state_dict['g_a.0.weight'].size(0)
        M = state_dict['g_a.6.weight'].size(0)
        net = cls(N, M)
        net.load_state_dict(state_dict)
        return net

    def compress(self, x):
        y = self.g_a(x)
        y_strings = self.entropy_bottleneck.compress(y)
        return {'strings': [y_strings], 'shape': y.size()[-2:]}

    def decompress(self, strings, shape):
        assert isinstance(strings, list) and len(strings) == 1
        y_hat = self.entropy_bottleneck.decompress(strings[0], shape)
        x_hat = self.g_s(y_hat)
        return {'x_hat': x_hat}


# From Balle's tensorflow compression examples
SCALES_MIN = 0.11
SCALES_MAX = 256
SCALES_LEVELS = 64


def get_scale_table(min=SCALES_MIN, max=SCALES_MAX, levels=SCALES_LEVELS):  # pylint: disable=W0622
    return torch.exp(torch.linspace(math.log(min), math.log(max), levels))


class ScaleHyperprior(CompressionModel):
    r"""Scale Hyperprior model from J. Balle, D. Minnen, S. Singh, S.J. Hwang,
    N. Johnston: `"Variational Image Compression with a Scale Hyperprior"
    <https://arxiv.org/abs/1802.01436>`_ Int. Conf. on Learning Representations
    (ICLR), 2018.

    Args:
        N (int): Number of channels
        M (int): Number of channels in the expansion layers (last layer of the
            encoder and last layer of the hyperprior decoder)
    """
    def __init__(self, N, M, **kwargs):
        super().__init__(entropy_bottleneck_channels=N, **kwargs)

        self.g_a = nn.Sequential(
            conv(3, N),
            GDN(N),
            conv(N, N),
            GDN(N),
            conv(N, N),
            GDN(N),
            conv(N, M),
        )

        self.g_s = nn.Sequential(
            deconv(M, N),
            GDN(N, inverse=True),
            deconv(N, N),
            GDN(N, inverse=True),
            deconv(N, N),
            GDN(N, inverse=True),
            deconv(N, 3),
        )

        self.h_a = nn.Sequential(
            conv(M, N, stride=1, kernel_size=3),
            nn.ReLU(inplace=True),
            conv(N, N),
            nn.ReLU(inplace=True),
            conv(N, N),
        )

        self.h_s = nn.Sequential(
            deconv(N, N),
            nn.ReLU(inplace=True),
            deconv(N, N),
            nn.ReLU(inplace=True),
            conv(N, M, stride=1, kernel_size=3),
            nn.ReLU(inplace=True),
        )

        self.gaussian_conditional = GaussianConditional(None)
        self.N = int(N)
        self.M = int(M)

    def forward(self, x):
        y = self.g_a(x)
        z = self.h_a(torch.abs(y))
        z_hat, z_likelihoods = self.entropy_bottleneck(z)  #量化+定义速率失真损失
        scales_hat = self.h_s(z_hat)
        y_hat, y_likelihoods = self.gaussian_conditional(y, scales_hat)  #编码导出y_hat时，依然需要解z_hat然后产出y_hat
        x_hat = self.g_s(y_hat)

        return {
            'x_hat': x_hat,
            'likelihoods': {
                'y': y_likelihoods,
                'z': z_likelihoods
            },
        }

    def load_state_dict(self, state_dict):
        # Dynamically update the entropy bottleneck buffers related to the CDFs
        update_registered_buffers(self.entropy_bottleneck,
                                  'entropy_bottleneck',
                                  ['_quantized_cdf', '_offset', '_cdf_length'],
                                  state_dict)
        update_registered_buffers(
            self.gaussian_conditional, 'gaussian_conditional',
            ['_quantized_cdf', '_offset', '_cdf_length', 'scale_table'],
            state_dict)
        super().load_state_dict(state_dict)

    @classmethod
    def from_state_dict(cls, state_dict):
        """Return a new model instance from `state_dict`."""
        N = state_dict['g_a.0.weight'].size(0)
        M = state_dict['g_a.6.weight'].size(0)
        net = cls(N, M)
        net.load_state_dict(state_dict)
        return net

    def update(self, scale_table=None, force=False):
        if scale_table is None:
            scale_table = get_scale_table()
        self.gaussian_conditional.update_scale_table(scale_table, force=force)
        super().update(force=force)

    #重点
    def compress(self, x):
        y = self.g_a(x)
        z = self.h_a(torch.abs(y))

        z_strings = self.entropy_bottleneck.compress(z) #z直接量化+估计速率失真 ++ 熵编码
        z_hat = self.entropy_bottleneck.decompress(z_strings, z.size()[-2:]) #z解码后结果（压缩时仍需要）

        scales_hat = self.h_s(z_hat) #z解码后通过h_s的结果（压缩时仍需要）
        indexes = self.gaussian_conditional.build_indexes(scales_hat)
        y_strings = self.gaussian_conditional.compress(y, indexes)  #y ++ 熵编码 其中indexes已被z_hat影响过
        return {'strings': [y_strings, z_strings], 'shape': z.size()[-2:]}

    def decompress(self, strings, shape):
        assert isinstance(strings, list) and len(strings) == 2
        z_hat = self.entropy_bottleneck.decompress(strings[1], shape)
        scales_hat = self.h_s(z_hat)
        indexes = self.gaussian_conditional.build_indexes(scales_hat)  #同压缩，获得indexes，其中indexes已被z_hat影响过
        y_hat = self.gaussian_conditional.decompress(strings[0], indexes) #y ++ 熵解码 其中indexes已被z_hat影响过
        x_hat = self.g_s(y_hat).clamp_(0, 1) #通过g_s网络，获得估计图像
        return {'x_hat': x_hat}


class MeanScaleHyperprior(ScaleHyperprior):
    r"""Scale Hyperprior with non zero-mean Gaussian conditionals from D.
    Minnen, J. Balle, G.D. Toderici: `"Joint Autoregressive and Hierarchical
    Priors for Learned Image Compression" <https://arxiv.org/abs/1809.02736>`_,
    Adv. in Neural Information Processing Systems 31 (NeurIPS 2018).

    Args:
        N (int): Number of channels
        M (int): Number of channels in the expansion layers (last layer of the
            encoder and last layer of the hyperprior decoder)
    """
    def __init__(self, N, M, **kwargs):
        super().__init__(N, M, **kwargs)

        self.h_a = nn.Sequential(
            conv(M, N, stride=1, kernel_size=3),
            nn.LeakyReLU(inplace=True),
            conv(N, N),
            nn.LeakyReLU(inplace=True),
            conv(N, N),
        )

        self.h_s = nn.Sequential(
            deconv(N, M),
            nn.LeakyReLU(inplace=True),
            deconv(M, M * 3 // 2),
            nn.LeakyReLU(inplace=True),
            conv(M * 3 // 2, M * 2, stride=1, kernel_size=3),
        )

    def forward(self, x):
        y = self.g_a(x)
        z = self.h_a(y)
        z_hat, z_likelihoods = self.entropy_bottleneck(z)
        gaussian_params = self.h_s(z_hat)
        scales_hat, means_hat = gaussian_params.chunk(2, 1)
        y_hat, y_likelihoods = self.gaussian_conditional(y,
                                                         scales_hat,
                                                         means=means_hat)
        x_hat = self.g_s(y_hat)

        return {
            'x_hat': x_hat,
            'likelihoods': {
                'y': y_likelihoods,
                'z': z_likelihoods
            },
        }

    def compress(self, x):
        y = self.g_a(x)
        z = self.h_a(y)

        z_strings = self.entropy_bottleneck.compress(z)
        z_hat = self.entropy_bottleneck.decompress(z_strings, z.size()[-2:])

        gaussian_params = self.h_s(z_hat)
        scales_hat, means_hat = gaussian_params.chunk(2, 1)
        indexes = self.gaussian_conditional.build_indexes(scales_hat)
        y_strings = self.gaussian_conditional.compress(y,
                                                       indexes,
                                                       means=means_hat)
        return {'strings': [y_strings, z_strings], 'shape': z.size()[-2:]}

    def decompress(self, strings, shape):
        assert isinstance(strings, list) and len(strings) == 2
        z_hat = self.entropy_bottleneck.decompress(strings[1], shape)
        gaussian_params = self.h_s(z_hat)
        scales_hat, means_hat = gaussian_params.chunk(2, 1)
        indexes = self.gaussian_conditional.build_indexes(scales_hat)
        y_hat = self.gaussian_conditional.decompress(strings[0],
                                                     indexes,
                                                     means=means_hat)
        x_hat = self.g_s(y_hat).clamp_(0, 1)
        return {'x_hat': x_hat}


class JointAutoregressiveHierarchicalPriors(CompressionModel):
    r"""Joint Autoregressive Hierarchical Priors model from D.
    Minnen, J. Balle, G.D. Toderici: `"Joint Autoregressive and Hierarchical
    Priors for Learned Image Compression" <https://arxiv.org/abs/1809.02736>`_,
    Adv. in Neural Information Processing Systems 31 (NeurIPS 2018).

    Args:
        N (int): Number of channels
        M (int): Number of channels in the expansion layers (last layer of the
            encoder and last layer of the hyperprior decoder)
    """
    def __init__(self, N=192, M=192, **kwargs):
        super().__init__(entropy_bottleneck_channels=N, **kwargs)

        self.g_a = nn.Sequential(
            conv(3, N, kernel_size=5, stride=2),
            GDN(N),
            conv(N, N, kernel_size=5, stride=2),
            GDN(N),
            conv(N, N, kernel_size=5, stride=2),
            GDN(N),
            conv(N, M, kernel_size=5, stride=2),
        )

        self.g_s = nn.Sequential(
            deconv(M, N, kernel_size=5, stride=2),
            GDN(N, inverse=True),
            deconv(N, N, kernel_size=5, stride=2),
            GDN(N, inverse=True),
            deconv(N, N, kernel_size=5, stride=2),
            GDN(N, inverse=True),
            deconv(N, 3, kernel_size=5, stride=2),
        )

        self.h_a = nn.Sequential(
            conv(M, N, stride=1, kernel_size=3),
            nn.LeakyReLU(inplace=True),
            conv(N, N, stride=2, kernel_size=5),
            nn.LeakyReLU(inplace=True),
            conv(N, N, stride=2, kernel_size=5),
        )

        self.h_s = nn.Sequential(
            deconv(N, M, stride=2, kernel_size=5),
            nn.LeakyReLU(inplace=True),
            deconv(M, M * 3 // 2, stride=2, kernel_size=5),
            nn.LeakyReLU(inplace=True),
            conv(M * 3 // 2, M * 2, stride=1, kernel_size=3),
        )

        self.entropy_parameters = nn.Sequential(
            nn.Conv2d(M * 12 // 3, M * 10 // 3, 1),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(M * 10 // 3, M * 8 // 3, 1),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(M * 8 // 3, M * 6 // 3, 1),
        )

        self.context_prediction = MaskedConv2d(M,
                                               2 * M,
                                               kernel_size=5,
                                               padding=2,
                                               stride=1)

        self.gaussian_conditional = GaussianConditional(None)
        self.N = int(N)
        self.M = int(M)

    def forward(self, x):
        y = self.g_a(x)
        z = self.h_a(y)
        z_hat, z_likelihoods = self.entropy_bottleneck(z)
        params = self.h_s(z_hat)

        y_hat = self.gaussian_conditional._quantize(  # pylint: disable=protected-access
            y, 'noise' if self.training else 'dequantize')
        ctx_params = self.context_prediction(y_hat)
        gaussian_params = self.entropy_parameters(
            torch.cat((params, ctx_params), dim=1))
        scales_hat, means_hat = gaussian_params.chunk(2, 1)
        _, y_likelihoods = self.gaussian_conditional(y,
                                                     scales_hat,
                                                     means=means_hat)
        x_hat = self.g_s(y_hat)

        return {
            'x_hat': x_hat,
            'likelihoods': {
                'y': y_likelihoods,
                'z': z_likelihoods
            },
        }

    @classmethod
    def from_state_dict(cls, state_dict):
        """Return a new model instance from `state_dict`."""
        N = state_dict['g_a.0.weight'].size(0)
        M = state_dict['g_a.6.weight'].size(0)
        net = cls(N, M)
        net.load_state_dict(state_dict)
        return net

    def compress(self, x):
        y = self.g_a(x)
        z = self.h_a(y)

        z_strings = self.entropy_bottleneck.compress(z)
        z_hat = self.entropy_bottleneck.decompress(z_strings, z.size()[-2:])

        params = self.h_s(z_hat)

        s = 4  # scaling factor between z and y
        kernel_size = 5  # context prediction kernel size
        padding = (kernel_size - 1) // 2

        y_height = z_hat.size(2) * s
        y_width = z_hat.size(3) * s

        y_hat = F.pad(y, (padding, padding, padding, padding))

        # yapf: enable
        # pylint: disable=protected-access
        cdf = self.gaussian_conditional._quantized_cdf.tolist()
        cdf_lengths = self.gaussian_conditional._cdf_length.reshape(-1).int().tolist()
        offsets = self.gaussian_conditional._offset.reshape(-1).int().tolist()
        # pylint: enable=protected-access

        y_strings = []
        for i in range(y.size(0)):
            encoder = BufferedRansEncoder()
            # Warning, this is slow...
            # TODO: profile the calls to the bindings...
            for h in range(y_height):
                for w in range(y_width):
                    y_crop = y_hat[i:i + 1, :, h:h + kernel_size, w:w + kernel_size]
                    ctx_params = self.context_prediction(y_crop)

                    # 1x1 conv for the entropy parameters prediction network, so
                    # we only keep the elements in the "center"
                    ctx_p = ctx_params[i:i + 1, :, padding:padding + 1, padding:padding + 1]
                    p = params[i:i + 1, :, h:h + 1, w:w + 1]
                    gaussian_params = self.entropy_parameters(torch.cat((p, ctx_p), dim=1))
                    scales_hat, means_hat = gaussian_params.chunk(2, 1)

                    indexes = self.gaussian_conditional.build_indexes(scales_hat)
                    y_q = torch.round(y_crop - means_hat)
                    y_hat[i, :, h + padding, w + padding] = (y_q + means_hat)[i, :, padding, padding]
                    encoder.encode_with_indexes(
                        y_q[i, :, padding, padding].int().tolist(),
                        indexes[i, :].squeeze().int().tolist(),
                        cdf,
                        cdf_lengths,
                        offsets)
            string = encoder.flush()
            y_strings.append(string)
        # yapf: disable

        return {'strings': [y_strings, z_strings], 'shape': z.size()[-2:]}

    def decompress(self, strings, shape):
        assert isinstance(strings, list) and len(strings) == 2
        # FIXME: we don't respect the default entropy coder and directly call the
        # range ANS decoder

        z_hat = self.entropy_bottleneck.decompress(strings[1], shape)
        params = self.h_s(z_hat)

        s = 4  # scaling factor between z and y
        kernel_size = 5  # context prediction kernel size
        padding = (kernel_size - 1) // 2

        y_height = z_hat.size(2) * s
        y_width = z_hat.size(3) * s

        # initialize y_hat to zeros, and pad it so we can directly work with
        # sub-tensors of size (N, C, kernel size, kernel_size)
        # yapf: disable
        y_hat = torch.zeros((z_hat.size(0), self.M, y_height + 2 * padding, y_width + 2 * padding),
                            device=z_hat.device)
        decoder = RansDecoder()

        # pylint: disable=protected-access
        cdf = self.gaussian_conditional._quantized_cdf.tolist()
        cdf_lengths = self.gaussian_conditional._cdf_length.reshape(-1).int().tolist()
        offsets = self.gaussian_conditional._offset.reshape(-1).int().tolist()

        # Warning: this is slow due to the auto-regressive nature of the
        # decoding... See more recent publication where they use an
        # auto-regressive module on chunks of channels for faster decoding...
        for i, y_string in enumerate(strings[0]):
            decoder.set_stream(y_string)

            for h in range(y_height):
                for w in range(y_width):
                    # only perform the 5x5 convolution on a cropped tensor
                    # centered in (h, w)
                    y_crop = y_hat[i:i + 1, :, h:h + kernel_size, w:w + kernel_size]
                    # ctx_params = self.context_prediction(torch.round(y_crop))
                    ctx_params = self.context_prediction(y_crop)

                    # 1x1 conv for the entropy parameters prediction network, so
                    # we only keep the elements in the "center"
                    ctx_p = ctx_params[i:i + 1, :, padding:padding + 1, padding:padding + 1]
                    p = params[i:i + 1, :, h:h + 1, w:w + 1]
                    gaussian_params = self.entropy_parameters(torch.cat((p, ctx_p), dim=1))
                    scales_hat, means_hat = gaussian_params.chunk(2, 1)

                    indexes = self.gaussian_conditional.build_indexes(scales_hat)

                    rv = decoder.decode_stream(
                        indexes[i, :].squeeze().int().tolist(),
                        cdf,
                        cdf_lengths,
                        offsets)
                    rv = torch.Tensor(rv).reshape(1, -1, 1, 1)

                    rv = self.gaussian_conditional._dequantize(rv, means_hat)

                    y_hat[i, :, h + padding:h + padding + 1, w + padding:w + padding + 1] = rv
        y_hat = y_hat[:, :, padding:-padding, padding:-padding]
        # pylint: enable=protected-access
        # yapf: enable

        x_hat = self.g_s(y_hat).clamp_(0, 1)
        return {'x_hat': x_hat}

    def update(self, scale_table=None, force=False):
        if scale_table is None:
            scale_table = get_scale_table()
        self.gaussian_conditional.update_scale_table(scale_table, force=force)
        super().update(force=force)

    def load_state_dict(self, state_dict):
        # Dynamically update the entropy bottleneck buffers related to the CDFs
        update_registered_buffers(self.entropy_bottleneck,
                                  'entropy_bottleneck',
                                  ['_quantized_cdf', '_offset', '_cdf_length'],
                                  state_dict)
        update_registered_buffers(
            self.gaussian_conditional, 'gaussian_conditional',
            ['_quantized_cdf', '_offset', '_cdf_length', 'scale_table'],
            state_dict)
        super().load_state_dict(state_dict)
