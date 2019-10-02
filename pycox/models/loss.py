import torch
from pycox.models.utils import pad_col


def _reduction(loss, reduction='mean'):
    if reduction == 'none':
        return loss
    elif reduction == 'mean':
        return loss.mean()
    elif reduction == 'sum':
        return loss.sum()
    raise ValueError(f"`reduction` = {reduction} is not valid. Use 'none', 'mean' or 'sum'.")


def nll_pmf(phi, idx_durations, events, reduction='mean', epsilon=1e-7):
    """Negative log-likelihood of the hazard parametrization model.
    See make_y for a better understanding of labeling scheeme.
    
    Arguments:
        phi {torch.tensor} -- Estimates in (-inf, inf), where hazard = sigmoid(phi).
        idx_durations {torch.tensor} -- Event times represented as indexes.
        events {torch.tensor} -- Indicator of event (1.) or censoring (0.).
            Same lenght as 'idx_durations'.
        reduction {string} -- How to reduce the loss.
            'none': No reduction.
            'mean': Mean of tensor.
            'sum: sum.
    
    Returns:
        torch.tensor -- Retruns mean negative log-likelihood.
    """
    if (idx_durations.max()) >= phi.shape[1]:
        raise ValueError("""'t_idx' too large. Probably need to increase output size of net.""")
    events = events.view(-1)
    idx_durations = idx_durations.view(-1, 1)
    phi = pad_col(phi)
    gamma = phi.max(1)[0]
    cumsum = phi.sub(gamma.view(-1, 1)).exp().cumsum(1)
    sum_ = cumsum[:, -1]
    part1 = phi.gather(1, idx_durations).view(-1).sub(gamma).mul(events)
    part2 = - sum_.relu().add(epsilon).log()
    part3 = sum_.sub(cumsum.gather(1, idx_durations).view(-1)).relu().add(epsilon).log().mul(1. - events)
    # need relu() in part3 (and possibly part2) because cumsum on gpu has some bugs and we risk getting negative numbers.
    loss = - part1.add(part2).add(part3)
    return _reduction(loss, reduction)


def _rank_loss_deephit(pmf, y, rank_mat, sigma, reduction='mean'):
    """Ranking loss from deephit.
    
    Arguments:
        pmf {torch.tensor} -- Matrix with probability mass function pmf_ij = f_i(t_j)
        y {torch.tensor} -- Matrix with indicator of duration and censoring time. 
        rank_mat {torch.tensor} -- See pair_rank_mat function.
        sigma {float} -- Sigma from DeepHit paper, choosen by you.
    
    Returns:
        torch.tensor -- loss
    """
    r = _diff_cdf_at_time_i(pmf, y)
    loss = rank_mat * torch.exp(-r/sigma)
    loss = loss.mean(1, keepdim=True)
    return _reduction(loss, reduction)

def _diff_cdf_at_time_i(pmf, y):
    """R is the matrix from the deephit code giving the difference in cdf between individual
    i and j, at the event time of j. 
    I.e: R_ij = F_i(T_i) - F_j(T_i)
    
    Arguments:
        pmf {torch.tensor} -- Matrix with probability mass function pmf_ij = f_i(t_j)
        y {torch.tensor} -- Matrix with indicator of duratio/censor time.

        # not_ec {torch.tensor} -- Indicator matrix with same shape as 'pmf', indicating
        #     that the individual has still not had an event or censoring.
    
    Returns:
        torch.tensor -- R_ij = F_i(T_i) - F_j(T_i)
    """
    n = pmf.shape[0]
    ones = torch.ones((n, 1), device=pmf.device)
    r = pmf.cumsum(1).matmul(y.transpose(0, 1))
    diag_r = r.diag().view(1, -1)
    r = ones.matmul(diag_r) - r
    return r.transpose(0, 1)

def rank_loss_deephit_single(phi, idx_durations, events, rank_mat, sigma, reduction='mean'):
    """Rank loss proposed by DeepHit authors for competing risks.
    
    Arguments:
        pmf {torch.tensor} -- Matrix with probability mass function pmf_ij = f_i(t_j)
        y {torch.tensor} -- Matrix with indicator of duration and censoring time. 
        rank_mat {torch.tensor} -- See pair_rank_mat function.
        sigma {float} -- Sigma from DeepHit paper, choosen by you.
    Arguments:
        phi {torch.tensor} -- Preditions as float tensor with shape [batch, n_durations]
            all in (-inf, inf).
        idx_durations {torch.tensor} -- Int tensor with index of durations.
        events {torch.tensor} -- Float indicator of event or censoring (1 is event).
        rank_mat {torch.tensor} -- See pair_rank_mat function.
        sigma {float} -- Sigma from DeepHit paper, choosen by you.
    
    Keyword Arguments:
        reduction {string} -- How to reduce the loss.
            'none': No reduction.
            'mean': Mean of tensor.
            'sum': sum.
    
    Returns:
        torch.tensor -- Rank loss.
    """
    idx_durations = idx_durations.view(-1, 1)
    events = events.float().view(-1)
    pmf = pad_col(phi).softmax(1)
    y = torch.zeros_like(pmf).scatter(1, idx_durations, 1.) # one-hot
    rank_loss = _rank_loss_deephit(pmf, y, rank_mat, sigma, reduction)
    return rank_loss

def nll_pmf_cr(phi, idx_durations, events, reduction='mean', epsilon=1e-7):
    """Negtive log-likelihood for pmf paraterizations. `phi` is the ''logit''.
    
    Arguments:
        phi {torch.tensor} -- Preditions as float tensor with shape [batch, n_risks, n_durations]
            all in (-inf, inf).
        idx_durations {torch.tensor} -- Int tensor with index of durations.
        events {torch.tensor} -- Int tensor with event types.
            {0: Censored, 1: first group, ..., n_risks: n'th risk group}.
    
    Keyword Arguments:
        reduction {string} -- How to reduce the loss.
            'none': No reduction.
            'mean': Mean of tensor.
            else: sum.
    
    Returns:
        torch.tensor -- Negative log-likelihood.
    """
    # Should improve numerical stbility by, e.g., log-sum-exp tric.
    events = events.view(-1) - 1
    event_01 = (events != -1).float()
    idx_durations = idx_durations.view(-1)
    batch_size = phi.size(0)
    sm = pad_col(phi.view(batch_size, -1)).softmax(1)[:, :-1].view(phi.shape)
    index = torch.arange(batch_size)
    part1 = sm[index, events, idx_durations].relu().add(epsilon).log().mul(event_01)
    part2 = (1 - sm.cumsum(2)[index, :, idx_durations].sum(1)).relu().add(epsilon).log().mul(1 - event_01)
    loss = - part1.add(part2)
    return _reduction(loss, reduction)

def rank_loss_deephit_cr(phi, idx_durations, events, rank_mat, sigma, reduction='mean'):
    """Rank loss proposed by DeepHit authors for competing risks.
    
    Arguments:
        phi {torch.tensor} -- Preditions as float tensor with shape [batch, n_risks, n_durations]
            all in (-inf, inf).
        idx_durations {torch.tensor} -- Int tensor with index of durations.
        events {torch.tensor} -- Int tensor with event types.
            {0: Censored, 1: first group, ..., n_risks: n'th risk group}.
        rank_mat {torch.tensor} -- See pair_rank_mat function.
        sigma {float} -- Sigma from DeepHit paper, choosen by you.
    
    Keyword Arguments:
        reduction {string} -- How to reduce the loss.
            'none': No reduction.
            'mean': Mean of tensor.
            else: sum.
    
    Returns:
        torch.tensor -- Rank loss.
    """
    idx_durations = idx_durations.view(-1)
    events = events.view(-1) - 1
    event_01 = (events == -1).float()

    batch_size, n_risks = phi.shape[:2]
    pmf = pad_col(phi.view(batch_size, -1)).softmax(1)
    pmf = pmf[:, :-1].view(phi.shape)
    y = torch.zeros_like(pmf)
    y[torch.arange(batch_size), :, idx_durations] = 1.

    loss = []
    for i in range(n_risks):
        rank_loss_i = _rank_loss_deephit(pmf[:, i, :], y[:, i, :], rank_mat, sigma, 'none')
        loss.append(rank_loss_i.view(-1) * (events == i).float())

    if reduction == 'none':
        return sum(loss)
    elif reduction == 'mean':
        return sum([lo.mean() for lo in loss])
    elif reduction == 'sum':
        return sum([lo.sum() for lo in loss])
    return _reduction(loss, reduction)


class _Loss(torch.nn.Module):
    def __init__(self, reduction='mean'):
        super().__init__()
        self.reduction = reduction


class NLLPMFLoss(_Loss):
    def forward(self, phi, idx_durations, events):
        return nll_pmf(phi, idx_durations, events, self.reduction)


class DeepHitSingleLoss(_Loss):
    """Loss for deephit (single risk) model.
    Alpha is  weighting between likelihood and rank loss (so not like in paper):

    loss = alpha * nll + (1 - alpha) rank_loss(sigma)
    
    Arguments:
        alpha {float} -- Weighting between likelihood and rank loss.
        sigma {float} -- Part of rank loss (see DeepHit paper)

    Keyword Arguments:
        reduction {string} -- How to reduce the loss.
            'none': No reduction.
            'mean': Mean of tensor.
            'sum': sum.
    """
    def __init__(self, alpha, sigma, reduction='mean'):
        super().__init__(reduction)
        self.alpha = alpha
        self.sigma = sigma

    def forward(self, phi, idx_durations, events, rank_mat):
        nll = nll_pmf(phi, idx_durations, events, self.reduction)
        rank_loss = rank_loss_deephit_single(phi, idx_durations, events, rank_mat, self.sigma,
                                             self.reduction)
        return self.alpha * nll + (1. - self.alpha) * rank_loss


class DeepHitLoss(_Loss):
    """Loss for deephit model.
    If you have only one event type, use LossDeepHitSingle instead!

    Alpha is  weighting between likelihood and rank loss (so not like in paper):

    loss = alpha * nll + (1 - alpha) rank_loss(sigma)
    
    Arguments:
        alpha {float} -- Weighting between likelihood and rank loss.
        sigma {float} -- Part of rank loss (see DeepHit paper)
    """
    def __init__(self, alpha, sigma, reduction='mean'):
        super().__init__(reduction)
        self.alpha = alpha
        self.sigma = sigma

    def forward(self, phi, idx_durations, events, rank_mat):
        nll =  nll_pmf_cr(phi, idx_durations, events, self.reduction)
        rank_loss = rank_loss_deephit_cr(phi, idx_durations, events, rank_mat, self.sigma, self.reduction)
        return self.alpha * nll + (1. - self.alpha) * rank_loss