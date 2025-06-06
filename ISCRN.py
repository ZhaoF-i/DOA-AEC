import torch.nn as nn
import torch.fft
import torch
from einops import rearrange
import torch.nn.functional as F
from nnstruct.s4d import S4D


class SCB(nn.Module):
    def __init__(self, in_channels=None, out_channels=None):
        super(SCB, self).__init__()
        self.conv_gate = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=(1, 1), stride=1,
                                   padding=(0, 0), dilation=1, groups=1, bias=True)
        self.conv_input = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=(1, 1), stride=1,
                                    padding=(0, 0), dilation=1, groups=1, bias=True)
        self.conv = nn.Conv2d(in_channels=out_channels, out_channels=out_channels, kernel_size=(3, 1), stride=1,
                              padding=(1, 0), dilation=1, groups=1, bias=True)
        # self.ceps_unit = CepsUnit(ch=out_channels)
        self.LN0 = LayerNorm(in_channels, f=161)
        self.LN1 = LayerNorm(out_channels, f=161)
        self.LN2 = LayerNorm(out_channels, f=161)
        self.S4D = S4D(161)

    def forward(self, x):

        b, c, f, t = x.shape
        y, _, compute = self.S4D(x.permute(0, 3, 1, 2).contiguous().view(b * t, c, f))  # input B,T,F
        x = y.contiguous().view(b, t, c, f).permute(0, 2, 3, 1) + x

        g = torch.sigmoid(self.conv_gate(self.LN0(x)))
        x = self.conv_input(x)
        y = self.conv(self.LN1(x*g))
        # y = y + self.ceps_unit(self.LN2((1 - g) * x))

        return y


class CepsUnit(nn.Module):
    def __init__(self, ch):
        super(CepsUnit, self).__init__()
        self.ch = ch
        self.ch_lstm_f = CH_LSTM_F(ch * 2, ch, ch * 2)
        self.LN = LayerNorm(ch * 2, f=81)

    def forward(self, x0):
        x0 = torch.fft.rfft(x0, 161, 2)
        x = torch.cat([x0.real, x0.imag], 1)
        x = self.ch_lstm_f(self.LN(x))
        x = x[:, :self.ch] + 1j * x[:, self.ch:]
        x = x * x0
        x = torch.fft.irfft(x, 161, 2)
        return x


class LayerNorm(nn.Module):
    def __init__(self, c, f):
        super(LayerNorm, self).__init__()
        self.w = nn.Parameter(torch.ones(1, c, f, 1))
        self.b = nn.Parameter(torch.rand(1, c, f, 1) * 1e-4)

    def forward(self, x):
        mean = x.mean([1, 2], keepdim=True)
        std = x.std([1, 2], keepdim=True)
        x = (x - mean) / (std + 1e-8) * self.w + self.b
        return x


class NET(nn.Module):
    def __init__(self, order=10, channels=20):
        super().__init__()
        self.act = nn.ELU()
        self.n_fft = 320
        self.hop_length = 161
        self.window = torch.hamming_window(self.n_fft)
        self.order = order

        self.in_ch_lstm = CH_LSTM_F(14, channels, channels)
        self.in_conv = nn.Conv2d(in_channels=14 + channels, out_channels=channels, kernel_size=(1, 1))
        self.cfb_e1 = SCB(channels, channels)
        self.cfb_e2 = SCB(channels, channels)
        self.cfb_e3 = SCB(channels, channels)
        self.cfb_e4 = SCB(channels, channels)
        self.cfb_e5 = SCB(channels, channels)

        self.ln = LayerNorm(channels, 161)
        self.ch_lstm = CH_LSTM_T(in_ch=channels, feat_ch=channels * 2, out_ch=channels, bi=True, num_layers=2)

        self.cfb_d5 = SCB(1 * channels, channels)
        self.cfb_d4 = SCB(2 * channels, channels)
        self.cfb_d3 = SCB(2 * channels, channels)
        self.cfb_d2 = SCB(2 * channels, channels)
        self.cfb_d1 = SCB(2 * channels, channels)

        self.out_ch_lstm = CH_LSTM_T(2 * channels, channels, channels * 2)
        self.out_conv = nn.Conv2d(in_channels=channels * 3, out_channels=2, kernel_size=(1, 1), padding=(0, 0),
                                  bias=True)

    def stft(self, x):
        b, m, t = x.shape[0], x.shape[1], x.shape[2],
        x = x.reshape(-1, t)
        X = torch.stft(x, n_fft=self.n_fft, hop_length=self.hop_length, window=self.window.to(x.device), return_complex=False)
        F, T = X.shape[1], X.shape[2]
        X = X.reshape(b, m, F, T, 2)
        X = torch.cat([X[..., 0], X[..., 1]], dim=1)
        return X

    def istft(self, Y, t):
        b, c, F, T = Y.shape
        m_out = int(c // 2)
        Y_r = Y[:, :m_out]
        Y_i = Y[:, m_out:]
        Y = torch.stack([Y_r, Y_i], dim=-1)
        Y = Y.reshape(-1, F, T, 2)
        y = torch.istft(torch.complex(Y[:,:,:,0],Y[:,:,:,1]), n_fft=self.n_fft, hop_length=self.hop_length, length=t, window=self.window.to(Y.device), return_complex=False)
        y = y.reshape(b, m_out, y.shape[-1])
        return y

    def forward(self, x):
        # x:[batch, channel, frequency, time]
        x = x.permute(0, 1, 3, 2)
        e0 = self.in_ch_lstm(x)
        e0 = self.in_conv(torch.cat([e0, x], 1))
        e1 = self.cfb_e1(e0)
        e2 = self.cfb_e2(e1)
        e3 = self.cfb_e3(e2)
        e4 = self.cfb_e4(e3)
        e5 = self.cfb_e5(e4)

        lstm_out = self.ch_lstm(self.ln(e5))
        # lstm_out = lstm_out.permute(0, 1, 3, 2)

        d5 = self.cfb_d5(torch.cat([e5 * lstm_out], dim=1))
        d4 = self.cfb_d4(torch.cat([e4, d5], dim=1))
        d3 = self.cfb_d3(torch.cat([e3, d4], dim=1))
        d2 = self.cfb_d2(torch.cat([e2, d3], dim=1))
        d1 = self.cfb_d1(torch.cat([e1, d2], dim=1))

        d0 = self.out_ch_lstm(torch.cat([e0, d1], dim=1))
        out = self.out_conv(torch.cat([d0, d1], dim=1))

        return out.permute(0, 1, 3, 2)

class CH_LSTM_T(nn.Module):
    def __init__(self, in_ch, feat_ch, out_ch, bi=False, num_layers=1):
        super().__init__()
        self.lstm2 = nn.LSTM(in_ch, feat_ch, num_layers=num_layers, batch_first=True, bidirectional=bi)
        self.bi = 1 if bi == False else 2
        self.linear = nn.Linear(self.bi * feat_ch, out_ch)
        self.out_ch = out_ch

    def forward(self, x):
        self.lstm2.flatten_parameters()
        b, c, f, t = x.shape
        x = rearrange(x, 'b c f t -> (b f) t c')
        x, _ = self.lstm2(x.float())
        x = self.linear(x)
        x = rearrange(x, '(b f) t c -> b c f t', b=b, f=f, t=t)
        return x


class CH_LSTM_F(nn.Module):
    def __init__(self, in_ch, feat_ch, out_ch, bi=True, num_layers=1):
        super().__init__()
        self.lstm2 = nn.LSTM(in_ch, feat_ch, num_layers=num_layers, batch_first=True, bidirectional=bi)
        self.linear = nn.Linear(2 * feat_ch, out_ch)
        self.out_ch = out_ch

    def forward(self, x):
        self.lstm2.flatten_parameters()
        b, c, f, t = x.shape
        x = rearrange(x, 'b c f t -> (b t) f c')
        x, _ = self.lstm2(x.float())
        x = self.linear(x)
        x = rearrange(x, '(b t) f c -> b c f t', b=b, f=f, t=t)
        return x


def complexity():
    # inputs = torch.randn(1,1,16100)
    model = NET()
    # output = model(inputs)
    # print(output.shape)

    from ptflops import get_model_complexity_info
    mac, param = get_model_complexity_info(model, (14, 100, 161), as_strings=True, print_per_layer_stat=True, verbose=True)
    print(mac, param)
    '''
    3.07 GMac + 0.57 = 3.64 GMac 950.8 k  
    '''


if __name__ == '__main__':
    complexity()


