import torch

# Samplers --------------------------------------------------------------------
class SimpleSampler():
    def __init__(self, diffuzz):
        self.current_step = -1
        self.diffuzz = diffuzz

    def __call__(self, *args, **kwargs):
        self.current_step += 1
        return self.step(*args, **kwargs)

    def init_x(self, shape):
        return torch.randn(*shape, device=self.diffuzz.device)

    def step(self, x, t, t_prev, noise):
        raise NotImplementedError("You should override the 'apply' function.")

class DDPMSampler(SimpleSampler):
    def step(self, x, t, t_prev, noise):
        alpha_cumprod = self.diffuzz._alpha_cumprod(t).view(t.size(0), *[1 for _ in x.shape[1:]])
        alpha_cumprod_prev = self.diffuzz._alpha_cumprod(t_prev).view(t_prev.size(0), *[1 for _ in x.shape[1:]])
        alpha = (alpha_cumprod / alpha_cumprod_prev)

        mu = (1.0 / alpha).sqrt() * (x - (1-alpha) * noise / (1-alpha_cumprod).sqrt())
        std = ((1-alpha) * (1. - alpha_cumprod_prev) / (1. - alpha_cumprod)).sqrt() * torch.randn_like(mu)
        return mu + std * (t_prev != 0).float().view(t_prev.size(0), *[1 for _ in x.shape[1:]])

class DDIMSampler(SimpleSampler):
    def step(self, x, t, t_prev, noise):
        alpha_cumprod = self.diffuzz._alpha_cumprod(t).view(t.size(0), *[1 for _ in x.shape[1:]])
        alpha_cumprod_prev = self.diffuzz._alpha_cumprod(t_prev).view(t_prev.size(0), *[1 for _ in x.shape[1:]])

        x0 = (x - (1 - alpha_cumprod).sqrt() * noise) / (alpha_cumprod).sqrt()
        dp_xt = (1 - alpha_cumprod_prev).sqrt()
        return (alpha_cumprod_prev).sqrt() * x0 + dp_xt * noise

class DPMSolverPlusPlusSampler(SimpleSampler):  # FIXME: CURRENTLY NOT WORKING
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.q_ts = {}

    def _get_coef(self, alpha_cumprod):
        log_alpha_t = alpha_cumprod.log()
        alpha_t = log_alpha_t.exp()
        sigma_t = (1-alpha_t ** 2).sqrt()
        lambda_t = log_alpha_t - sigma_t.log()
        return alpha_t, sigma_t, lambda_t

    def init_x(self, shape):
        alpha_cumprod = self.diffuzz._alpha_cumprod(torch.ones(shape[0], device=self.diffuzz.device)).view(-1, *[1 for _ in shape[1:]])
        return torch.randn(*shape, device=self.diffuzz.device) * self._get_coef(alpha_cumprod)[1]

    def step(self, x, t, t_prev, noise):
        alpha_cumprod = self.diffuzz._alpha_cumprod(t).view(t.size(0), *[1 for _ in x.shape[1:]])
        stride = (t_prev - t)
        if self.current_step == 0:
            alpha_t, sigma_t, _ = self._get_coef(alpha_cumprod)
        elif self.current_step == 1:
            alpha_cumprod_next = self.diffuzz._alpha_cumprod(t+stride).view(t.size(0), *[1 for _ in x.shape[1:]])
            alpha_t, sigma_t, lambda_t = self._get_coef(alpha_cumprod)
            _, sigma_t_next, lambda_t_next = self._get_coef(alpha_cumprod_next)
            h = lambda_t - lambda_t_next
            x = sigma_t / sigma_t_next * x - alpha_t * torch.expm1(-h) * self.q_ts[self.current_step-1]
        else:
            alpha_cumprod_next = self.diffuzz._alpha_cumprod(t+stride).view(t.size(0), *[1 for _ in x.shape[1:]])
            alpha_cumprod_next_next = self.diffuzz._alpha_cumprod(t+stride*2).view(t.size(0), *[1 for _ in x.shape[1:]])
            
            alpha_t, sigma_t, lambda_t = self._get_coef(alpha_cumprod)
            _, sigma_t_next, lambda_t_next = self._get_coef(alpha_cumprod_next)
            _, _, lambda_t_next_next = self._get_coef(alpha_cumprod_next_next)
            
            h = lambda_t - lambda_t_next
            h_next = lambda_t_next - lambda_t_next_next
            
            r = h_next / h
            D = (1 + 1 / (2 * r)) * self.q_ts[self.current_step-1] - 1 / (2 * r) * self.q_ts[self.current_step-2]
            x = sigma_t / sigma_t_next * x - alpha_t * torch.expm1(-h) * D
        self.q_ts[self.current_step] =  (x - sigma_t * noise) / alpha_t
        return x

sampler_dict = {
    'ddpm': DDPMSampler,
    'ddim': DDIMSampler,
    'dpmsolver++': DPMSolverPlusPlusSampler,
}

# Custom simplified foward/backward diffusion (cosine schedule)
class Diffuzz():
    def __init__(self, s=0.008, device="cpu", cache_steps=None, scaler=1):
        self.device = device
        self.s = torch.tensor([s]).to(device)
        self._init_alpha_cumprod = torch.cos(self.s / (1 + self.s) * torch.pi * 0.5) ** 2
        self.scaler = scaler
        self.cached_steps = None
        if cache_steps is not None:
            self.cached_steps = self._alpha_cumprod(torch.linspace(0, 1, cache_steps, device=device))

    def _alpha_cumprod(self, t):
        if self.cached_steps is None:
            if self.scaler > 1:
                t = 1 - (1-t) ** self.scaler
            elif self.scaler < 1:
                t = t ** self.scaler
            alpha_cumprod = torch.cos((t + self.s) / (1 + self.s) * torch.pi * 0.5) ** 2 / self._init_alpha_cumprod
            return alpha_cumprod.clamp(0.0001, 0.9999)
        else:
            return self.cached_steps[t.mul(len(self.cached_steps)-1).long()]

    def diffuse(self, x, t, noise=None): # t -> [0, 1]
        if noise is None:
            noise = torch.randn_like(x)
        alpha_cumprod = self._alpha_cumprod(t).view(t.size(0), *[1 for _ in x.shape[1:]])
        return alpha_cumprod.sqrt() * x + (1-alpha_cumprod).sqrt() * noise, noise

    def undiffuse(self, x, t, t_prev, noise, sampler=None):
        if sampler is None:
            sampler = DDPMSampler(self)
        return sampler(x, t, t_prev, noise)

    def sample(self, model, model_inputs, shape, mask=None, t_start=1.0, t_end=0.0, timesteps=20, x_init=None, cfg=3.0, unconditional_inputs=None, sampler='ddpm', half=False):
        r_range = torch.linspace(t_start, t_end, timesteps+1)[:, None].expand(-1, shape[0] if x_init is None else x_init.size(0)).to(self.device)            
        if isinstance(sampler, str):
            if sampler in sampler_dict:
                sampler = sampler_dict[sampler](self)
            else:
                raise ValueError(f"If sampler is a string it must be one of the supported samplers: {list(sampler_dict.keys())}")
        elif issubclass(sampler, SimpleSampler):
            sampler =  sampler(self)
        else:
            raise ValueError("Sampler should be either a string or a SimpleSampler object.")
        preds = []
        x = sampler.init_x(shape) if x_init is None or mask is not None else x_init.clone()
        if half:
            r_range = r_range.half()
            x = x.half()
        for i in range(0, timesteps):
            if mask is not None and x_init is not None:
                x_renoised, _ = self.diffuse(x_init, r_range[i])
                x = x * mask + x_renoised * (1-mask)
            pred_noise = model(x, r_range[i], **model_inputs)
            if cfg is not None:
                if unconditional_inputs is None:
                    unconditional_inputs = {k: torch.zeros_like(v) for k, v in model_inputs.items()}
                pred_noise_unconditional = model(x, r_range[i], **unconditional_inputs)
                pred_noise = torch.lerp(pred_noise_unconditional, pred_noise, cfg)
            x = self.undiffuse(x, r_range[i], r_range[i+1], pred_noise, sampler=sampler)
            preds.append(x)
        return preds
        
    def p2_weight(self, t, k=1.0, gamma=1.0):
        alpha_cumprod = self._alpha_cumprod(t)
        return (k + alpha_cumprod / (1 - alpha_cumprod)) ** -gamma