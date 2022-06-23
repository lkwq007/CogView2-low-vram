# -*- encoding: utf-8 -*-
'''
@File    :   direct_sr.py
@Time    :   2022/03/02 13:58:11
@Author  :   Ming Ding 
@Contact :   dm18@mails.tsinghua.edu.cn
'''

# here put the import lib
import os
import sys
import math
import random
import torch

# -*- encoding: utf-8 -*-
'''
@File    :   inference_cogview2.py
@Time    :   2021/10/10 16:31:34
@Author  :   Ming Ding 
@Contact :   dm18@mails.tsinghua.edu.cn
'''

# here put the import lib
import os
import sys
import math
import random
from PIL import ImageEnhance, Image

import torch
import argparse
from torchvision import transforms

from SwissArmyTransformer import get_args
from SwissArmyTransformer.training.model_io import load_checkpoint
from .dsr_sampling import filling_sequence_dsr, IterativeEntfilterStrategy
from SwissArmyTransformer.generation.utils import timed_name, save_multiple_images, generate_continually

from .dsr_model import DsrModel

from icetk import icetk as tokenizer

class DirectSuperResolution:
    def __init__(self, args, path, max_bz=4, shared_transformer=None):
        args.load = path
        args.kernel_size = 5
        args.kernel_size2 = 5
        args.new_sequence_length = 4624
        args.layout = [96,496,4096]
        
        model = DsrModel(args, transformer=shared_transformer)
        if args.fp16:
            model = model.half()
        
        load_checkpoint(model, args) # on cpu
        model.eval()
        self.model = model
        # if not args.single_gpu:
        #     self.model = model.cuda(1)
        # else:
        #     self.model = model.cuda()

        # save cpu weights
        self.saved_weights = dict((k,v.clone()) 
            for k, v in model.named_parameters()
            if 'transformer' in k
        )
        
        invalid_slices = [slice(tokenizer.num_image_tokens, None)]
    
        self.strategy = IterativeEntfilterStrategy(invalid_slices,
            temperature=args.temp_all_dsr, topk=args.topk_dsr, temperature2=args.temp_cluster_dsr) # temperature not used
        self.max_bz = max_bz
        if not args.single_gpu:
            self.strategy.cluster_labels = self.strategy.cluster_labels.cuda(1)
    
    def _restore_transformer_from_cpu(self, non_blocking=False):
        for k, v in self.model.named_parameters():
            if k in self.saved_weights:
                v.copy_(self.saved_weights[k], non_blocking=non_blocking)
        
    def __call__(self, text_tokens, image_tokens, enhance=False):
        if len(text_tokens.shape) == 1:
            text_tokens.unsqueeze_(0)
        if len(image_tokens.shape) == 1:
            image_tokens.unsqueeze_(0)
        # =====================   Debug   ======================== #
        # new_image_tokens = []
        # for small_img in image_tokens:
        #     decoded = tokenizer.decode(image_ids=small_img)
        #     decoded = torch.nn.functional.interpolate(decoded, size=(480, 480)).squeeze(0)
        #     ndarr = decoded.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).to('cpu', torch.uint8).numpy()
        #     image_pil_raw = ImageEnhance.Sharpness(Image.fromarray(ndarr))
        #     small_img2 = tokenizer.encode(image_pil=image_pil_raw.enhance(1.5), image_size=480).view(-1)
        #     new_image_tokens.append(small_img2)
        # image_tokens = torch.stack(new_image_tokens)
        # return image_tokens
        # ===================== END OF BLOCK ======================= #
        if enhance:
            new_image_tokens = []
            for small_img in image_tokens:
                decoded = tokenizer.decode(image_ids=small_img).squeeze(0)
                ndarr = decoded.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).to('cpu', torch.uint8).numpy()
                image_pil_raw = ImageEnhance.Sharpness(Image.fromarray(ndarr))
                small_img2 = tokenizer.encode(image_pil=image_pil_raw.enhance(1.), image_size=160).view(-1)
                new_image_tokens.append(small_img2)
            image_tokens = torch.stack(new_image_tokens)
                
        seq = torch.cat((text_tokens,image_tokens), dim=1)
        seq1 = torch.tensor([tokenizer['<start_of_image>']]*3601, device=image_tokens.device).unsqueeze(0).expand(text_tokens.shape[0], -1)
        print('Converting Dsr model...')
        self._restore_transformer_from_cpu()
        model = self.model
        print('Direct super-resolution...')
        output_list = []
        for tim in range(max(text_tokens.shape[0] // self.max_bz, 1)): 
            output1 = filling_sequence_dsr(model,
                seq[tim*self.max_bz:(tim+1)*self.max_bz], 
                seq1[tim*self.max_bz:(tim+1)*self.max_bz], 
                warmup_steps=1, block_hw=(1, 0),
                strategy=self.strategy
                )
            output_list.extend(output1[1:])
        return torch.cat(output_list, dim=0)