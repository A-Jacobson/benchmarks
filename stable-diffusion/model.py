import torch
import torch.nn.functional as F
from composer.models import ComposerModel
from torchmetrics import MeanSquaredError, Metric, MetricCollection
import diffusers
from diffusers import AutoencoderKL, DDPMScheduler, StableDiffusionPipeline, UNet2DConditionModel, LMSDiscreteScheduler
from diffusers.utils.import_utils import is_xformers_available
from transformers import CLIPTextModel, CLIPTokenizer
from tqdm.auto import tqdm
from PIL import Image


class StableDiffusion(ComposerModel):
    """Latent diffusion conditioned on text prompts that are run through a pre-trained CLIP or LLM model. The CLIP outputs 
    passed to as an additional input to our Unet during training and can later be used to guide the image generation process.
    Args:
        unet: huggingface conditional unet, must accept a (B, C, H, W) input, (B,) timestep array of noise timesteps, and (B, 77, 768) text conditioning vectors.
        vae: huggingface or compatible vae. must support `.encode()` and `decode()` functions.
        text_encoder: hugginface clip or llm.
        tokenizer: tokenizer used for text_encoder. commonly clip tokenizer
        noise_scheduler: huggingface diffusers noise scheduler.
        loss_fn: torch loss function, Default: `F.mse_loss`.
        train_text_encoder(bool): It can be helpful to train the text encoder for fine-tuning. Default: `False`.
        train_metrics(list): list of torchmetrics to calculate during training. Default: `MeanSquaredError()`.
        val_metrics(list): list of torchmetrics to calculate during validation. Default: `MeanSquaredError()`.
    """
    def __init__(self,
                 unet: torch.nn.Module,
                 vae: torch.nn.Module,
                 text_encoder: torch.nn.Module,
                 tokenizer: callable,
                 noise_scheduler: diffusers.schedulers,
                 inference_scheduler: diffusers.schedulers,
                 pipeline: diffusers.pipelines,
                 loss_fn: callable = F.mse_loss,
                 train_text_encoder: bool = False,
                 train_metrics: list = [MeanSquaredError()],
                 val_metrics: list = [MeanSquaredError()],
                 image_key: str = 'image_tensor',
                 caption_key: str = 'input_ids'):
        super().__init__()
        self.unet = unet
        self.vae = vae
        self.text_encoder = text_encoder
        self.tokenizer = tokenizer
        self.noise_scheduler = noise_scheduler
        self.inference_scheduler = inference_scheduler

        # freeze vae during diffusion training
        self.vae.requires_grad_(False)
        # freeze text_encoder during diffusion training
        if not train_text_encoder:
            self.text_encoder.requires_grad_(False)

        self.loss_fn = loss_fn

        self.train_metrics = MetricCollection(train_metrics)
        self.val_metrics = MetricCollection(val_metrics)

        self.pipeline = pipeline
        self.image_key = image_key
        self.caption_key = caption_key

    def forward(self, batch):
        inputs, conditioning = batch[self.image_key], batch[self.caption_key]

        # Encode the images to the latent space. This is slow, we should cache the results
        latents = self.vae.encode(inputs)['latent_dist'].sample().data
        # Magical scaling number (See https://github.com/huggingface/diffusers/issues/437#issuecomment-1241827515)
        latents *= 0.18215

        # Encode the text. Assume that the text is already tokenized. This is slow, we should cache the results.
        conditioning = self.text_encoder(conditioning)[0]  # (batch_size, 77, 768)

        # Sample the diffusion timesteps
        timesteps = torch.randint(0, len(self.noise_scheduler), (latents.shape[0], ), device=latents.device)
        # Add noise to the inputs (forward diffusion)
        noise = torch.randn_like(latents)

        noised_latents = self.noise_scheduler.add_noise(
            latents, noise, timesteps)

        # Get the target for loss depending on the prediction type
        if self.noise_scheduler.config.prediction_type == "epsilon":
            target = noise
        elif self.noise_scheduler.config.prediction_type == "v_prediction":
            target = self.noise_scheduler.get_velocity(latents, noise,
                                                       timesteps)
        else:
            raise ValueError(
                f"Unknown prediction type {self.noise_scheduler.config.prediction_type}"
            )
        # Forward through the model
        return self.unet(noised_latents, timesteps,
                         conditioning)['sample'], target

    def loss(self, outputs, batch):
        """loss between unet output and added noise, typically mse"""
        return self.loss_fn(outputs[0], outputs[1])

    def eval_forward(self, batch, outputs=None):
        if outputs is not None:
            return outputs
        return self.forward(batch)

    # def generate(self,
    #              prompt: list[str],
    #              height: int = None,
    #              width: int = None,
    #              num_inference_steps: int = 50,
    #              guidance_scale: float = 7.5,
    #              negative_prompt: list[str] = None,
    #              num_images_per_prompt: int = 1,
    #              eta: float = 1):
    #     """Generate images from noise using the backward diffusion process.
    #     Args:
    #         prompt (str or List[str]) — The prompt or prompts to guide the image generation.
    #         height (int, optional, defaults to self.unet.config.sample_size * self.vae_scale_factor) — The height in pixels of the generated image.
    #         width (int, optional, defaults to self.unet.config.sample_size * self.vae_scale_factor) — The width in pixels of the generated image.
    #         num_inference_steps (int, optional, defaults to 50) — The number of denoising steps. More denoising steps usually lead to a higher quality image at the expense of slower inference.
    #         guidance_scale (float, optional, defaults to 7.5) — Guidance scale as defined in Classifier-Free Diffusion Guidance. guidance_scale is defined as w of equation 2. of Imagen Paper. Guidance scale is enabled by setting guidance_scale > 1. Higher guidance scale encourages to generate images that are closely linked to the text prompt, usually at the expense of lower image quality.
    #         negative_prompt (str or List[str], optional) — The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored if guidance_scale is less than 1).
    #         num_images_per_prompt (int, optional, defaults to 1) — The number of images to generate per prompt.
    #         eta (float, optional, defaults to 0.0) — Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to schedulers.DDIMScheduler, will be ignored for others.
    #     """
    #     height = height or self.unet.config.sample_size * self.vae_scale_factor
    #     width = width or self.unet.config.sample_size * self.vae_scale_factor

    #     return self.pipeline(prompt=prompt,
    #                          height=height,
    #                          width=width,
    #                          num_inference_steps=num_inference_steps,
    #                          guidance_scale=guidance_scale,
    #                          negative_prompt=negative_prompt,
    #                          num_images_per_prompt=num_images_per_prompt,
    #                          eta=eta)

    @torch.no_grad()
    def generate(self,
            prompt: list[str],
            height: int = None,
            width: int = None,
            num_inference_steps: int = 50,
            guidance_scale: float = 7.5,
            negative_prompt: list[str] = None,
            num_images_per_prompt: int = 1,
            eta: float = 1):

        batch_size = 1
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor
        generator = torch.manual_seed(32)   # Seed generator to create the inital latent noise

        device = self.vae.device

        # encode prompt + unconidtional input
        text_input = self.tokenizer(prompt, padding="max_length", max_length=self.tokenizer.model_max_length, truncation=True, return_tensors="pt")
        uncond_input = self.tokenizer([""] * batch_size, padding="max_length", max_length=self.tokenizer.model_max_length, return_tensors="pt")

        # concat these into one batch to avoid 2x forwards?
        text_embeddings = self.text_encoder(text_input.input_ids.to(device))[0]
        uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(device))[0]   

        # concat uncond + prompt
        text_embeddings = torch.cat([uncond_embeddings, text_embeddings])


        # prepare for diffusion generation process
        latents = torch.randn((batch_size, self.unet.in_channels, height // 8, width // 8), generator=generator, device=device)
        self.inference_scheduler.set_timesteps(num_inference_steps)

        # The K-LMS scheduler needs to multiply the `latents` by its `sigma` values. Let's do this here
        latents = latents * self.inference_scheduler.init_noise_sigma

        # backward diffusion process
        for t in tqdm(self.inference_scheduler.timesteps):
            # expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
            latent_model_input = torch.cat([latents] * 2)

            latent_model_input = self.inference_scheduler.scale_model_input(latent_model_input, t)

            # predict the noise residual
            noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample

            # perform guidance
            noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = self.inference_scheduler.step(noise_pred, t, latents).prev_sample

        # We now use the vae to decode the generated latents back into the image.
        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample
        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.detach().cpu().permute(0, 2, 3, 1).numpy()
        images = (image * 255).round().astype("uint8")
        return [Image.fromarray(image) for image in images]


    def get_metrics(self, is_train: bool = False):
        if is_train:
            metrics = self.train_metrics
        else:
            metrics = self.val_metrics

        if isinstance(metrics, Metric):
            metrics_dict = {metrics.__class__.__name__: metrics}
        else:
            metrics_dict = {}
            for name, metric in metrics.items():
                assert isinstance(metric, Metric)
                metrics_dict[name] = metric

        return metrics_dict

    def update_metric(self, batch, outputs, metric):
        metric.update(outputs[0], outputs[1])


def build_stable_diffusion_model(model_name_or_path: str,
                                 train_text_encoder:bool=False,
                                 image_key: str = 'image_tensor',
                                 caption_key: str = 'input_ids'):
    """
    Args:
        model_name_or_path(str): commonly "CompVis/stable-diffusion-v1-4" or "stabilityai/stable-diffusion-2-1"

    """
    model_name_or_path = "CompVis/stable-diffusion-v1-4"
    unet = UNet2DConditionModel.from_pretrained(model_name_or_path,
                                                subfolder='unet')
    if is_xformers_available():
        unet.enable_xformers_memory_efficient_attention()
    vae = AutoencoderKL.from_pretrained(model_name_or_path, subfolder='vae')
    text_encoder = CLIPTextModel.from_pretrained(model_name_or_path,
                                                 subfolder='text_encoder')
    noise_scheduler = DDPMScheduler.from_pretrained(model_name_or_path,
                                                    subfolder='scheduler')
    inference_scheduler = LMSDiscreteScheduler.from_pretrained(model_name_or_path, subfolder="scheduler")
    tokenizer = CLIPTokenizer.from_pretrained(model_name_or_path,
                                              subfolder="tokenizer")
    pipeline = StableDiffusionPipeline.from_pretrained(model_name_or_path,
                                       text_encoder=text_encoder,
                                       vae=vae,
                                       unet=unet)
    return StableDiffusion(unet=unet,
                           vae=vae,
                           text_encoder=text_encoder,
                           tokenizer=tokenizer,
                           noise_scheduler=noise_scheduler,
                           inference_scheduler=inference_scheduler,
                           pipeline=pipeline,
                           train_text_encoder=train_text_encoder,
                           image_key=image_key,
                           caption_key=caption_key)