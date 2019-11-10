import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

class CNNLayer(nn.Module):
    def __init__(self, n_in, num_filters=(128,), motif_len=(7,), pool_size=(1,), dilation=1, dropout_rate=0.0):
        super(CNNLayer, self).__init__()
        conv = []
        pool = []
        for n_out, ksize, p in zip(num_filters, motif_len, pool_size):
            conv.append(nn.Conv1d(n_in, n_out, kernel_size=ksize, dilation=2**dilation, padding=2**dilation*(ksize//2)))
            if p > 1:
                pool.append(nn.MaxPool1d(p, stride=1, padding=p//2))
            else:
                pool.append(nn.Identity())
            n_in = n_out
        self.conv = nn.ModuleList(conv)
        self.pool = nn.ModuleList(pool)
        self.dropout = nn.Dropout(p=dropout_rate)

    def forward(self, x): # (B=1, 4, N)
        for conv, pool in zip(self.conv, self.pool):
            x = self.dropout(F.relu(pool(conv(x)))) # (B, num_filters, N)
        return x


class CNNLSTMEncoder(nn.Module):
    def __init__(self, n_in, lstm_cnn=False,
            num_filters=(256,), motif_len=(7,), pool_size=(1,), dilation=0,
            num_lstm_layers=0, num_lstm_units=0, dropout_rate=0.0):
        super(CNNLSTMEncoder, self).__init__()
        self.n_in = self.n_out = n_in
        self.lstm_cnn = lstm_cnn
        if num_lstm_layers == 0 and num_lstm_units > 0:
            num_lstm_layers = 1

        self.dropout = nn.Dropout(p=dropout_rate)
        self.conv = self.lstm = None

        if not lstm_cnn and len(num_filters) > 0 and num_filters[0] > 0:
            self.conv = CNNLayer(n_in, num_filters, motif_len, pool_size, dilation, dropout_rate=dropout_rate)
            self.n_out = n_in = num_filters[-1]

        if num_lstm_layers > 0:
            self.lstm = nn.LSTM(n_in, num_lstm_units, num_layers=num_lstm_layers, batch_first=True, bidirectional=True, 
                            dropout=dropout_rate if num_lstm_layers>1 else 0)
            self.n_out = n_in = num_lstm_units*2

        if lstm_cnn and len(num_filters) > 0 and num_filters[0] > 0:
            self.conv = CNNLayer(n_in, num_filters, motif_len, pool_size, dilation, dropout_rate=dropout_rate)
            self.n_out = n_in = num_filters[-1]

        if self.conv is None and self.lstm is None:
            self.n_out *= 3


    def forward(self, x): # (B, n_in, N)
        if self.conv is None and self.lstm is None:
            x = torch.transpose(x, 1, 2) # (B, N, C)
            return x, x, x

        if self.conv is not None and not self.lstm_cnn:
            x = self.conv(x) # (B, C, N)
        # B, C, N = x.shape
        x = torch.transpose(x, 1, 2) # (B, N, C)

        if self.lstm is not None:
            x, _ = self.lstm(x)
            x = self.dropout(F.relu(x)) # (B, N, H*2)

        if self.conv is not None and self.lstm_cnn:
            x = torch.transpose(x, 1, 2) # (B, H*2, N)
            x = self.conv(x) # (B, C, N)
            x = torch.transpose(x, 1, 2) # (B, N, C)

        assert(x.shape[-1] % 3 == 0)
        x_l = x[:, :, 0::3]
        x_r = x[:, :, 1::3]
        x_r = x_r[:, :, torch.arange(x_r.shape[-1]-1, -1, -1)] # reverse the last axis
        x_u = x[:, :, 2::3]
        return x_l, x_r, x_u # (B, N, n_out//3) * 3


class FCPairedLayer(nn.Module):
    def __init__(self, n_in, n_out=1, layers=(), dropout_rate=0.0, context=1, join='cat'):
        super(FCPairedLayer, self).__init__()
        self.dropout = nn.Dropout(p=dropout_rate)
        self.context = context
        self.join = join
        if len(layers)>0 and layers[0]==0:
            layers = ()

        if join=='cat':
            n = n_in*context*2 # concat
        else:
            n = n_in*context # add or mul
        
        linears = []
        for m in layers:
            linears.append(nn.Linear(n, m))
            n = m
        linears.append(nn.Linear(n, n_out))
        self.fc = nn.ModuleList(linears)

    def forward(self, x_l, x_r=None):
        x_r = x_l if x_r is None else x_r
        assert(x_l.shape == x_r.shape)
        B, N, C = x_l.shape

        x_l = x_l.view(B, N, 1, C).expand(B, N, N, C)
        x_r = x_r.view(B, 1, N, C).expand(B, N, N, C)
        if self.join=='cat':
            v = torch.cat((x_l, x_r), dim=3) # (B, N, N, C*2)
        elif self.join=='add':
            v = x_l + x_r # (B, N, N, C)
        elif self.join=='mul':
            v = x_l * x_r # (B, N, N, C)

        if self.context > 1:
            z = [v]
            for d in range(1, self.context // 2 + 1):
                z_u = torch.zeros_like(v)
                z_u[:, d:, :-d, :] = v[:, :-d, d:, :] # (i-d, j+d)
                z.append(z_u)
                z_d = torch.zeros_like(v)
                z_d[:, :-d, d:, :] = v[:, d:, :-d, :] # (i+d, j-d)
                z.append(z_d)
            v = torch.cat(z, dim=3) # (B, N, N, C*width)

        v = v.view(B*N*N, -1)
        for fc in self.fc[:-1]:
            v = self.dropout(F.relu(fc(v)))
        y = self.fc[-1](v) # (B*N*N, n_out)
        y = y.view(B, N, N, -1)

        return y


class BilinearPairedLayer(nn.Module):
    def __init__(self, n_in, n_hidden, n_out, context=1, dropout_rate=0.0):
        super(BilinearPairedLayer, self).__init__()
        self.context = context
        self.linear = nn.Linear(n_in, n_hidden)
        self.bilinear = nn.Bilinear(n_hidden*context, n_hidden*context, n_out)
        self.dropout = nn.Dropout(p=dropout_rate)

    def forward(self, x):
        B, N, _ = x.shape
        x = x.view(B*N, -1) # (B*N, n_in)
        x = self.dropout(F.relu(self.linear(x)))
        x = x.view(B, N, -1) # (B, N, n_hidden)
        w = self.context // 2
        z = []
        for d in range(-w, w+1):
            y = torch.diag(torch.ones(N-abs(d)), diagonal=d).to(x.device) # (N, N)
            y = y.view(1, N, N).expand(B, N, N) # (B, N, N)
            y = torch.bmm(y, x) # (B, N, n_hidden)
            z.append(y)
        v = torch.cat(z, dim=2) # (B, N, n_hidden*width)
        H = v.shape[2]
        v_l = v.view(B, N, 1, H).expand(B, N, N, H).reshape(B*N*N, H)
        v_r = v.view(B, 1, N, H).expand(B, N, N, H).reshape(B*N*N, H)
        return self.bilinear(v_l, v_r).view(B, N, N, -1) # (B, n_out)


class FCUnpairedLayer(nn.Module):
    def __init__(self, n_in, n_out=1, layers=(), dropout_rate=0.0, context=1):
        super(FCUnpairedLayer, self).__init__()
        self.dropout = nn.Dropout(p=dropout_rate)
        self.context = context
        if len(layers)>0 and layers[0]==0:
            layers = ()

        n = n_in * context
        linears = []
        for m in layers:
            linears.append(nn.Linear(n, m))
            n = m
        linears.append(nn.Linear(n, n_out))
        self.fc = nn.ModuleList(linears)

    def forward(self, x):
        B, N, C = x.shape

        if self.context > 1:
            z = [x]
            for d in range(1, self.context // 2 + 1):
                z_u = torch.zeros_like(x)
                z_u[:, d:, :] = x[:, :-d, :] # i-d
                z.append(z_u)
                z_d = torch.zeros_like(x)
                z_d[:, :-d, :] = x[:, d:, :] # i+d
                z.append(z_d)
            x = torch.cat(z, dim=2) # (B, N, C*width)

        x = x.view(B*N, -1) # (B*N, C*width)
        for fc in self.fc[:-1]:
            x = F.relu(fc(x))
            x = self.dropout(x)
        x = self.fc[-1](x) # (B*N, n_out)
        return x.view(B, N, -1) # (B, N, n_out)


class FCLengthLayer(nn.Module):
    def __init__(self, n_in, layers=(), dropout_rate=0.5):
        super(FCLengthLayer, self).__init__()
        self.dropout = nn.Dropout(p=dropout_rate)
        self.n_in = n_in
        n = n_in if isinstance(n_in, int) else np.prod(n_in)
        linears = []
        for m in layers:
            linears.append(nn.Linear(n, m))
            n = m
        linears.append(nn.Linear(n, 1))
        self.linears = nn.ModuleList(linears)

        if isinstance(self.n_in, int):
            self.x = torch.tril(torch.ones((self.n_in, self.n_in)))
        else:
            n = np.prod(self.n_in)
            x = np.fromfunction(lambda i, j, k, l: np.logical_and(k<=i ,l<=j), (*self.n_in, *self.n_in))
            self.x = torch.from_numpy(x.astype(np.float32)).reshape(n, n)


    def forward(self, x): 
        for l in self.linears[:-1]:
            x = F.relu(l(x))
            x = self.dropout(x)
        return self.linears[-1](x)


    def make_param(self):
        x = self.forward(self.x.to(self.linears[-1].weight.device))
        return x.reshape((self.n_in,) if isinstance(self.n_in, int) else self.n_in)
