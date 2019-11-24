import torch
import torch.nn as nn
import torch.nn.functional as F
from .layers import (BilinearPairedLayer, CNNLayer, CNNLSTMEncoder,
                     CNNPairedLayer, CNNUnpairedLayer, FCLengthLayer,
                     FCPairedLayer, FCUnpairedLayer)
from .embedding import OneHotEmbedding, SparseEmbedding


class AbstractFold(nn.Module):
    def __init__(self, predict, 
            n_out_paired_layers, n_out_unpaired_layers,
            embed_size=0,
            num_filters=(256,), filter_size=(7,), dilation=0, pool_size=(1,), 
            num_lstm_layers=0, num_lstm_units=0, num_hidden_units=(128,), no_split_lr=False,
            dropout_rate=0.0, fc_dropout_rate=0.0, fc='linear', num_att=0,
            lstm_cnn=False, context_length=1, mix_base=0, pair_join='cat'):
        super(AbstractFold, self).__init__()
        self.predict = predict
        self.mix_base = mix_base
        self.embedding = OneHotEmbedding() if embed_size == 0 else SparseEmbedding(embed_size)
        n_in_base = self.embedding.n_out
        n_in = n_in_base

        self.encoder = CNNLSTMEncoder(n_in, lstm_cnn=lstm_cnn, 
            num_filters=num_filters, filter_size=filter_size, pool_size=pool_size, dilation=dilation, num_att=num_att,
            num_lstm_layers=num_lstm_layers, num_lstm_units=num_lstm_units, dropout_rate=dropout_rate, no_split_lr=no_split_lr)
        n_in = self.encoder.n_out

        self.fc_paired = self.fc_unpaired = self.softmax = None
        if pair_join=='bilinear':
            if n_out_paired_layers > 0:
                self.fc_paired = BilinearPairedLayer(n_in//3, n_out_paired_layers, 
                                        layers=num_hidden_units, dropout_rate=fc_dropout_rate, 
                                        context=context_length, n_in_base=n_in_base, mix_base=self.mix_base)
            if n_out_unpaired_layers > 0:
                self.fc_unpaired = FCUnpairedLayer(n_in//3, n_out_unpaired_layers,
                                        layers=num_hidden_units, dropout_rate=fc_dropout_rate, 
                                        context=context_length, n_in_base=n_in_base, mix_base=self.mix_base)

        elif fc=='linear':
            if n_out_paired_layers > 0:
                self.fc_paired = FCPairedLayer(n_in//3, n_out_paired_layers,
                                        layers=num_hidden_units, dropout_rate=fc_dropout_rate, 
                                        context=context_length, join=pair_join, n_in_base=n_in_base, mix_base=self.mix_base)
            if n_out_unpaired_layers > 0:
                self.fc_unpaired = FCUnpairedLayer(n_in//3, n_out_unpaired_layers,
                                        layers=num_hidden_units, dropout_rate=fc_dropout_rate, 
                                        context=context_length, n_in_base=n_in_base, mix_base=self.mix_base)

        elif fc=='conv':
            if n_out_paired_layers > 0:
                self.fc_paired = CNNPairedLayer(n_in//3, n_out_paired_layers,
                                        layers=num_hidden_units, dropout_rate=fc_dropout_rate, 
                                        context=context_length, join=pair_join, n_in_base=n_in_base, mix_base=self.mix_base)
            if n_out_unpaired_layers > 0:
                self.fc_unpaired = CNNUnpairedLayer(n_in//3, n_out_unpaired_layers,
                                        layers=num_hidden_units, dropout_rate=fc_dropout_rate, 
                                        context=context_length, n_in_base=n_in_base, mix_base=self.mix_base)

        elif fc=='profile':
            if n_out_unpaired_layers > 0:
                self.fc_unpaired = FCUnpairedLayer(n_in//3, n_out_unpaired_layers, layers=num_hidden_units, dropout_rate=fc_dropout_rate)
                self.softmax = nn.Softmax(dim=2)

        else:
            raise('not implemented')


    def clear_count(self, param):
        param_count = {}
        for n, p in param.items():
            if n.startswith("score_"):
                param_count["count_"+n[6:]] = torch.zeros_like(p)
        param.update(param_count)
        return param


    def calculate_differentiable_score(self, v, param, count):
        s = 0
        for n, p in param.items():
            if n.startswith("score_"):
                s += torch.sum(p * count["count_"+n[6:]].to(p.device))
        s += v - s.item()
        return s


    def forward(self, seq, max_internal_length=30, constraint=None, reference=None,
            loss_pos_paired=0.0, loss_neg_paired=0.0, loss_pos_unpaired=0.0, loss_neg_unpaired=0.0):
        param = self.make_param(seq)
        ss = []
        preds = []
        pairs = []
        for i in range(len(seq)):
            param_on_cpu = { k: v.to("cpu") for k, v in param[i].items() }
            with torch.no_grad():
                v, pred, pair = self.predict(seq[i], self.clear_count(param_on_cpu),
                            max_internal_length=max_internal_length if max_internal_length is not None else len(seq[i]),
                            constraint=constraint[i] if constraint is not None else '', 
                            reference=reference[i] if reference is not None else '', 
                            loss_pos_paired=loss_pos_paired, loss_neg_paired=loss_neg_paired,
                            loss_pos_unpaired=loss_pos_unpaired, loss_neg_unpaired=loss_neg_unpaired)
            if torch.is_grad_enabled():
                v = self.calculate_differentiable_score(v, param[i], param_on_cpu)
            ss.append(v)
            preds.append(pred)
            pairs.append(pair)
        ss = torch.stack(ss) if torch.is_grad_enabled() else torch.tensor(ss)
        return ss, preds, pairs


    def sinkhorn(self, score_basepair, score_unpair):

        def sinkhorn_(A, n_iter=4):
            """
            Sinkhorn iterations.

            :param A: (n_batches, d, d) tensor
            :param n_iter: Number of iterations.
            """
            for i in range(n_iter):
                A /= A.sum(dim=1, keepdim=True)
                A /= A.sum(dim=2, keepdim=True)
            return A

        w = torch.triu(score_basepair, diagonal=1)
        w = w + w.transpose(1, 2) 
        w = w + torch.diag_embed(score_unpair)
        w = sinkhorn_(w)
        score_unpair = torch.diagonal(w, dim1=1, dim2=2)
        w = torch.triu(w, diagonal=1)
        score_basepair = w + w.transpose(1, 2) 

        return score_basepair, score_unpair
