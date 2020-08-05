import math
from tqdm import trange, tqdm
import numpy as np
import wandb
import torch
import torch.optim as optim
from torch.optim.lr_scheduler import MultiStepLR, CosineAnnealingWarmRestarts
from torch.nn.functional import normalize
import torch.backends.cudnn as cudnn
import torch.nn as nn

from model import get_model, get_head
from cfg import get_cfg
from eval_sgd import eval_sgd
from eval_knn import eval_knn
from datasets import get_ds


if __name__ == "__main__":
    cfg = get_cfg()
    cfg.byol = True
    wrun = wandb.init(project="white_ss", config=cfg)

    ds = get_ds(cfg.dataset)(cfg.bs, cfg)
    model, out_size = get_model(cfg.arch, cfg.dataset)
    params = list(model.parameters())
    head = get_head(out_size, cfg)
    params += list(head.parameters())
    pred = nn.Sequential(
        nn.Linear(cfg.emb, cfg.head_size),
        nn.BatchNorm1d(cfg.head_size),
        nn.ReLU(),
        nn.Linear(cfg.head_size, cfg.emb),
    )
    pred = pred.cuda().train()
    params += list(pred.parameters())

    model_t, _ = get_model(cfg.arch, cfg.dataset)
    head_t = get_head(out_size, cfg)

    optimizer = optim.Adam(
        params, lr=cfg.lr, betas=(cfg.adam_b0, 0.999), weight_decay=cfg.adam_l2
    )
    if cfg.lr_step == "cos":
        scheduler = CosineAnnealingWarmRestarts(
            optimizer, T_0=cfg.T0, T_mult=cfg.Tmult, eta_min=cfg.eta_min
        )
    elif cfg.lr_step == "step":
        scheduler = MultiStepLR(optimizer, milestones=cfg.drop, gamma=cfg.drop_gamma)

    def update_target(tau):
        for t, s in zip(model_t.parameters(), model.parameters()):
            t.data.copy_(t.data * tau + s.data * (1.0 - tau))
        for t, s in zip(head_t.parameters(), head.parameters()):
            t.data.copy_(t.data * tau + s.data * (1.0 - tau))

    def loss_fn(x, y):
        x = normalize(x, dim=-1, p=2)
        y = normalize(y, dim=-1, p=2)
        return 2 - 2 * (x * y).sum(dim=-1).mean()

    update_target(0)

    bs = cfg.bs
    lr_warmup = 0 if cfg.lr_warmup else 500
    cudnn.benchmark = True
    for ep in trange(cfg.epoch, position=0):
        loss_ep = []
        iters = len(ds.train)
        for n_iter, (samples, _) in enumerate(tqdm(ds.train, position=1)):
            if lr_warmup < 500:
                lr_scale = (lr_warmup + 1) / 500
                for pg in optimizer.param_groups:
                    pg["lr"] = cfg.lr * lr_scale
                lr_warmup += 1

            optimizer.zero_grad()
            z = [pred(head(model(x))) for x in samples]
            with torch.no_grad():
                zt = [head_t(model_t(x)) for x in samples]

            loss = 0
            for i in range(len(samples) - 1):
                for j in range(i + 1, len(samples)):
                    loss += loss_fn(z[i], zt[j]) + loss_fn(z[j], zt[i])

            loss /= sum(range(len(samples)))
            loss.backward()
            optimizer.step()
            loss_ep.append(loss.item())

            tau = 1 - (1 - cfg.byol_tau) * (math.cos(math.pi * ep / cfg.epoch) + 1) / 2
            update_target(tau)

            if cfg.lr_step == "cos" and lr_warmup >= 500:
                scheduler.step(ep + n_iter / iters)

        if cfg.lr_step == "step":
            scheduler.step()

        if (ep + 1) % cfg.eval_every == 0:
            acc_knn = eval_knn(model, out_size, ds.clf, ds.test, cfg.knn)
            acc = eval_sgd(model, out_size, ds.clf, ds.test, 500)
            wandb.log({"acc": acc, "acc_knn": acc_knn}, commit=False)
            model.train()

        wandb.log({"loss": np.mean(loss_ep), "ep": ep})