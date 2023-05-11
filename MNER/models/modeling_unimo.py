from typing import Any, Optional, Tuple
import math

import torch
from torch import nn, Tensor, device
from copy import deepcopy
import torch.nn.functional as F
from transformers.activations import ACT2FN
from transformers.modeling_utils import (
    PreTrainedModel,
    apply_chunking_to_forward,
)
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_outputs import BaseModelOutput, BaseModelOutputWithPooling


# some function
def get_extended_attention_mask(attention_mask: Tensor, input_shape: Tuple[int], device: device) -> Tensor:
    """
    Makes broadcastable attention and causal masks so that future and masked tokens are ignored.

    Arguments:
        attention_mask (:obj:`torch.Tensor`):
            Mask with ones indicating tokens to attend to, zeros for tokens to ignore.
        input_shape (:obj:`Tuple[int]`):
            The shape of the input to the model.
        device: (:obj:`torch.device`):
            The device of the input to the model.

    Returns:
        :obj:`torch.Tensor` The extended attention mask, with a the same dtype as :obj:`attention_mask.dtype`.
    """
    # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
    # ourselves in which case we just need to make it broadcastable to all heads.
    if attention_mask.dim() == 3:
        extended_attention_mask = attention_mask[:, None, :, :]
    elif attention_mask.dim() == 2:
        # Provided a padding mask of dimensions [batch_size, seq_length]
        # - if the model is a decoder, apply a causal mask in addition to the padding mask
        # - if the model is an encoder, make the mask broadcastable to [batch_size, num_heads, seq_length, seq_length]
        extended_attention_mask = attention_mask[:, None, None, :]
    else:
        raise ValueError(
            f"Wrong shape for input_ids (shape {input_shape}) or attention_mask (shape {attention_mask.shape})"
        )

    # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
    # masked positions, this operation will create a tensor which is 0.0 for
    # positions we want to attend and -10000.0 for masked positions.
    # Since we are adding it to the raw scores before the softmax, this is
    # effectively the same as removing these entirely.
    extended_attention_mask = extended_attention_mask.to(dtype=torch.long)  # fp16 compatibility
    extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
    return extended_attention_mask


def get_head_mask(
        head_mask: Optional[Tensor], num_hidden_layers: int, is_attention_chunked: bool = False
) -> Tensor:
    """
    Prepare the head mask if needed.

    Args:
        head_mask (:obj:`torch.Tensor` with shape :obj:`[num_heads]` or :obj:`[num_hidden_layers x num_heads]`, `optional`):
            The mask indicating if we should keep the heads or not (1.0 for keep, 0.0 for discard).
        num_hidden_layers (:obj:`int`):
            The number of hidden layers in the model.
        is_attention_chunked: (:obj:`bool`, `optional`, defaults to :obj:`False`):
            Whether or not the attentions scores are computed by chunks or not.

    Returns:
        :obj:`torch.Tensor` with shape :obj:`[num_hidden_layers x batch x num_heads x seq_length x seq_length]` or
        list with :obj:`[None]` for each layer.
    """
    head_mask = [None] * num_hidden_layers

    return head_mask


# models

class CLIPVisionEmbeddings(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.embed_dim = config.hidden_size
        self.image_size = config.image_size
        self.patch_size = config.patch_size

        self.class_embedding = nn.Parameter(torch.randn(self.embed_dim))

        self.patch_embedding = nn.Conv2d(
            in_channels=3, out_channels=self.embed_dim, kernel_size=self.patch_size, stride=self.patch_size, bias=False
        )

        self.num_patches = (self.image_size // self.patch_size) ** 2
        self.num_positions = self.num_patches + 1
        self.position_embedding = nn.Embedding(self.num_positions, self.embed_dim)
        self.register_buffer("position_ids", torch.arange(self.num_positions).expand((1, -1)))

        self.aux_position_embedding = nn.Embedding(48, self.embed_dim)
        self.register_buffer("aux_position_ids", torch.arange(48).expand((1, -1)))

        self.rcnn_position_embedding = nn.Embedding(12, self.embed_dim)
        self.register_buffer("rcnn_position_ids", torch.arange(12).expand((1, -1)))

    def forward(self, pixel_values, aux_embeddings=None, rcnn_embeddings=None):
        batch_size = pixel_values.shape[0]

        class_embeds = self.class_embedding.expand(batch_size, 1, -1)
        embeddings = class_embeds

        if aux_embeddings is not None:
            aux_embeds = []
            for aux_embedding in aux_embeddings:
                aux_embed = self.patch_embedding(aux_embedding)
                aux_embed = aux_embed.flatten(2).transpose(1, 2).flatten(0, 1)
                aux_embeds.append(aux_embed)
            aux_embeds = torch.stack(aux_embeds)  # bsz, 48, 768
            aux_embeds = aux_embeds + self.aux_position_embedding(self.aux_position_ids)
            embeddings = torch.cat((embeddings, aux_embeds), dim=1)

        if rcnn_embeddings is not None:
            rcnn_embeds = []
            for rcnn_embedding in rcnn_embeddings:
                rcnn_embed = self.patch_embedding(rcnn_embedding)
                rcnn_embed = rcnn_embed.flatten(2).transpose(1, 2).flatten(0, 1)
                rcnn_embeds.append(rcnn_embed)
            rcnn_embeds = torch.stack(rcnn_embeds)  # bsz, 12, 768
            rcnn_embeds = rcnn_embeds + self.rcnn_position_embedding(self.rcnn_position_ids)
            embeddings = torch.cat((embeddings, rcnn_embeds), dim=1)
        return embeddings


class BertEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""
    embedding = None
    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        # position_ids (1, len position emb) is contiguous in memory and exported when serialized
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        self.register_buffer("position_ids", torch.arange(config.max_position_embeddings).expand((1, -1)))

    def forward(
            self, input_ids=None, token_type_ids=None, position_ids=None, inputs_embeds=None, past_key_values_length=0
    ):
        if input_ids is not None:
            input_shape = input_ids.size()
        else:
            input_shape = inputs_embeds.size()[:-1]

        seq_length = input_shape[1]

        if position_ids is None:
            position_ids = self.position_ids[:, past_key_values_length: seq_length + past_key_values_length]

        # Setting the token_type_ids to the registered buffer in constructor where it is all zeros, which usually occurs
        # when its auto-generated, registered buffer helps users when tracing the model without passing token_type_ids, solves
        # issue #5664
        if token_type_ids is None:
            if hasattr(self, "token_type_ids"):
                buffered_token_type_ids = self.token_type_ids[:, :seq_length]
                buffered_token_type_ids_expanded = buffered_token_type_ids.expand(input_shape[0], seq_length)
                token_type_ids = buffered_token_type_ids_expanded
            else:
                token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=self.position_ids.device)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = inputs_embeds + token_type_embeddings
        if self.position_embedding_type == "absolute":
            position_embeddings = self.position_embeddings(position_ids)
            BertEmbeddings.embedding = position_embeddings      # Author Add
            embeddings += position_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings

# Designed Module 1
class ShareKey():
    # need to initialize config/v_max_l/t_max_l/layer, and then call init()
    config = None
    v_max_l, t_max_l = None, None
    num_heads = None
    head_dim = None
    key, vision_bias, text_bias = None, None, None
    text_bn, vision_bn = None, None
    layer = None
    device = 'cuda'

    @classmethod
    def init(cls):
        print("ShareKey Init")
        initializer = nn.init.xavier_uniform_
        # initializer = nn.init.xavier_normal_
        embed_dim = cls.config.hidden_size
        cls.num_heads = cls.config.num_attention_heads
        cls.head_dim = embed_dim // cls.num_heads
        assert cls.head_dim * cls.num_heads == embed_dim
        cls.key = []
        cls.vision_bias = []
        cls.text_bias = []
        cls.text_bn = []
        cls.vision_bn = []
        for l in range(cls.layer):
            cls.key.append(
                nn.Parameter(
                    initializer(torch.empty([1, cls.num_heads, cls.head_dim, 1], device=cls.device))
                )
            )
            cls.vision_bias.append(
                nn.Parameter(
                    initializer(torch.empty([cls.num_heads, cls.v_max_l, cls.v_max_l], device=cls.device))
                )
            )
            cls.text_bias.append(
                nn.Parameter(
                    initializer(torch.empty([cls.num_heads, cls.t_max_l, cls.t_max_l], device=cls.device))
                )
            )
            cls.text_bn.append(nn.BatchNorm2d(cls.t_max_l))
            cls.vision_bn.append(nn.BatchNorm2d(cls.v_max_l))

    @classmethod
    def cal_attention(cls, query_states, layer, modality='text', shape=3):
        # print("layer:",layer," key:", cls.key[layer])
        # query_states: [bs*num_heads, length, head_dim]
        assert modality in ['text', 'vision']
        assert shape in [3, 4]
        if shape == 4:
            query_states = query_states.reshape(query_states.shape[0] * query_states.shape[1], query_states.shape[2],
                                                query_states.shape[3])
        assert query_states.shape[0] % cls.num_heads == 0
        bsz = query_states.shape[0] // cls.num_heads

        query_states = query_states.view(bsz, cls.num_heads, -1, cls.head_dim)  # [bsz, num_heads, length, head_dim]
        bias = cls.vision_bias[layer] if modality == 'vision' else cls.text_bias[layer]  # [num_heads, length, length]
        bn = cls.vision_bn[layer] if modality == 'vision' else cls.text_bn[layer]

        # query: [bsz, num_heads, length, head_dim] key: [1, num_heads, head_dim, 1]
        attn_weights = torch.matmul(query_states, cls.key[layer])  # [bsz, num_heads, length, 1]
        attn_weights = attn_weights.expand(bsz, cls.num_heads, attn_weights.shape[2],
                                           attn_weights.shape[2])  # [bsz, num_heads, length, length]

        attn_weights = attn_weights + bias.unsqueeze(0)  # [bsz, num_heads, length, length]

        if shape == 3:
            return attn_weights.reshape(bsz * cls.num_heads, attn_weights.shape[2],
                                        attn_weights.shape[2])  # [bsz*num_heads, length, length]
        elif shape == 4:
            return attn_weights  # [bsz, num_heads, length, length]

# Not Use
class CurrentClassifier(nn.Module):
    def __init__(self, in_features, n_class, bias=True, kaiming_init=False):
        super(CurrentClassifier, self).__init__()
        self.classifier = nn.Linear(in_features=in_features, out_features=n_class, bias=bias)
        if kaiming_init == True:
            nn.init.kaiming_normal_(self.classifier.weight, nonlinearity="linear")
        self.mlp_head = nn.utils.spectral_norm(self.classifier)

    def forward(self, x):
        return self.mlp_head(x)

# Not Use
class AllClassifier(nn.Module):

    def __init__(self, in_features, n_class, bias=True, kaiming_init=False):
        super(AllClassifier, self).__init__()
        self.classifier = nn.Linear(in_features=in_features, out_features=n_class, bias=bias)
        if kaiming_init == True:
            nn.init.kaiming_normal_(self.classifier.weight, nonlinearity="linear")
        self.mlp_head = nn.utils.spectral_norm(self.classifier)

    def forward(self, x):
        return self.mlp_head(x)

class VisionClassifier(nn.Module):
    def __init__(self, in_feature, n_class):
        super(VisionClassifier, self).__init__()
        self.dense = nn.Linear(in_feature, n_class)
        self.activation = nn.Softmax(dim=-1)

    def forward(self, hidden_states):
        first_token_tensor = hidden_states[:, 0]  # 选取出[cls]
        pooled_output = self.dense(first_token_tensor)
        # pooled_output = self.activation(pooled_output)
        return pooled_output

class TextClassifier(nn.Module):
    def __init__(self, in_feature, n_class):
        super(TextClassifier, self).__init__()
        self.dense = nn.Linear(in_feature, n_class)
        self.activation = nn.Softmax(dim=-1)

    def forward(self, hidden_states):
        first_token_tensor = hidden_states[:, 0]  # 选取出[cls]
        pooled_output = self.dense(first_token_tensor)
        # pooled_output = self.activation(pooled_output)
        return pooled_output

class CatClassifier(nn.Module):
    def __init__(self, in_feature, n_class):
        super(CatClassifier, self).__init__()
        self.dense = nn.Linear(in_feature, n_class)
        self.activation = nn.Softmax(dim=-1)
        self.dropout = nn.Dropout(0.1)

    def forward(self, vision_seq_in_text, text_seq):
        # [bs, len1, hidden]
        cat_tensor = torch.cat((vision_seq_in_text, text_seq), dim=-1)
        cat_tensor = self.dropout(cat_tensor)
        pooled_output = self.dense(cat_tensor)
        # pooled_output = self.activation(pooled_output)
        return pooled_output

# Not Use
class FFN(nn.Module):
    def __init__(self, hidden_size, intermediate_size, activate="gelu"):
        super().__init__()
        self.activation_fn = ACT2FN[activate]
        self.fc1 = nn.Linear(hidden_size, intermediate_size)
        self.fc2 = nn.Linear(intermediate_size, hidden_size)

    def forward(self, hidden_states):
        hidden_states = self.fc1(hidden_states)
        hidden_states = self.activation_fn(hidden_states)
        hidden_states = self.fc2(hidden_states)
        return hidden_states

# Not Use
class GradKeyAndBias():
    _key, key, _grad_key = None, None, None
    _bias_text, bias_text, _grad_bias_text = None, None, None
    _bias_vision, bias_vision, _grad_bias_vision = None, None, None
    start_layer, end_layer = -1, -1  # [start_layer, end_layer]  左闭右闭

    @classmethod
    def init(cls):
        cls.key = []
        cls.bias_text = []
        cls.bias_vision = []
        cls._grad_key, cls._grad_bias_vision, cls._grad_bias_text = [], [], []
        for i in range(cls.end_layer + 1):
            cls.key.append(ShareKey.key[i].clone())
            cls.bias_text.append(ShareKey.text_bias[i].clone())
            cls.bias_vision.append(ShareKey.vision_bias[i].clone())
            cls._grad_key.append(0)
            cls._grad_bias_text.append(0)
            cls._grad_bias_vision.append(0)

    @classmethod
    def _function(cls, _grad, _state, state, layer):
        return torch.norm(_grad[layer] * (state[layer] - _state[layer]), p=1)

    @classmethod
    def save_params(cls):
        cls._key = []
        cls._bias_text = []
        cls._bias_vision = []
        for i in range(cls.start_layer):
            cls._key.append(0)
            cls._bias_text.append(0)
            cls._bias_vision.append(0)
        for layer in range(cls.start_layer, cls.end_layer + 1):
            cls._key.append(ShareKey.key[layer].detach().clone())
            cls._bias_text.append(ShareKey.text_bias[layer].detach().clone())
            cls._bias_vision.append(ShareKey.vision_bias[layer].detach().clone())

    @classmethod
    def store_grad(cls, grad_key, grad_bias_text, grad_bias_vision):
        cls._grad_key = grad_key
        cls._grad_bias_text = grad_bias_text
        cls._grad_bias_vision = grad_bias_vision

    @classmethod
    def calculate(cls):
        if cls._key == None or cls.key == None:
            return None
        key, bias_vision, bias_text = 0, 0, 0
        for layer in range(cls.start_layer, cls.end_layer + 1):
            key += cls._function(cls._grad_key, cls._key, ShareKey.key, layer)
            bias_vision += cls._function(cls._grad_bias_vision, cls._bias_vision, ShareKey.vision_bias, layer)
            bias_text += cls._function(cls._grad_bias_text, cls._bias_text, ShareKey.text_bias, layer)
        return key + bias_vision + bias_text

# Designed Module 2
class AttentionReg():
    old_model = None
    old_key = None
    old_text_bias = None
    old_vision_bias = None

    attention_text_list = []
    attention_vision_list = []

    zero = torch.as_tensor([0.]).cuda()

    @classmethod
    def clear_attention_list(cls):
        cls.attention_vision_list = []
        cls.attention_text_list = []

    @classmethod
    def update_old_model(cls, model):
        # Copy model in last task
        if cls.old_model != None:
            del cls.old_model
        with torch.no_grad():
            cls.old_model = deepcopy(model)

    @classmethod
    def update_old_key_and_bias(cls):
        if cls.old_key != None:
            del cls.old_key
            del cls.old_text_bias
            del cls.old_vision_bias
        cls.old_key = []
        cls.old_text_bias = []
        cls.old_vision_bias = []
        for i in range(ShareKey.layer):
            cls.old_key.append(ShareKey.key[i].detach().clone())
            cls.old_text_bias.append(ShareKey.text_bias[i].detach().clone())
            cls.old_vision_bias.append(ShareKey.vision_bias[i].detach().clone())

    @classmethod
    def change_ShareKey(cls):
        _key = ShareKey.key
        _text_bias = ShareKey.text_bias
        _vision_bias = ShareKey.vision_bias
        ShareKey.key = cls.old_key
        ShareKey.text_bias = cls.old_text_bias
        ShareKey.vision_bias = cls.old_vision_bias
        cls.old_key = _key
        cls.old_text_bias = _text_bias
        cls.old_vision_bias = _vision_bias

    @classmethod
    def cal_loss(cls, old_attention_list, attention_list, merge_type="height"):
        assert len(old_attention_list) == len(attention_list)
        layers_to_pool = range(len(old_attention_list))
        totloss = 0

        for idx, (a, b) in enumerate(zip(old_attention_list, attention_list)):
            assert a.shape == b.shape  # [bs, max_len, max_len]
            if merge_type == "height":
                a = a.sum(dim=1).view(a.shape[0], -1)  # [bs, max_len]
                b = b.sum(dim=1).view(b.shape[0], -1)  # [bs, max_len]
            elif merge_type == "width":
                a = a.sum(dim=2).view(a.shape[0], -1)  # [bs, max_len]
                b = b.sum(dim=2).view(b.shape[0], -1)  # [bs, max_len]
            diff = a - b
            asym_choice = torch.nn.ReLU(inplace=True)
            relu_out = asym_choice(diff)
            layer_loss = torch.mean(torch.frobenius_norm(F.normalize(relu_out, dim=1, p=2))) / 100.0
            distance_loss_weight = 1
            layer_loss = layer_loss * distance_loss_weight
            totloss += layer_loss
        return totloss / len(layers_to_pool)


START_SHARE_LAYER = 9  # share from layer 9
# START_SHARE_LAYER = 20  # Not Share
# _START_SHARE_LAYER = 9  # using at ablation
# START_SHARE_LAYER = -1  # Share All

# ----------------------------------------------------------------------------------------------------------------------

class CLIPAttention(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.embed_dim = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.embed_dim // self.num_heads
        assert (
                self.head_dim * self.num_heads == self.embed_dim
        ), f"embed_dim must be divisible by num_heads (got `embed_dim`: {self.embed_dim} and `num_heads`: {self.num_heads})."
        self.scale = self.head_dim ** -0.5
        self.dropout = config.attention_dropout

        self.k_proj = nn.Linear(self.embed_dim, self.embed_dim)
        self.v_proj = nn.Linear(self.embed_dim, self.embed_dim)
        self.q_proj = nn.Linear(self.embed_dim, self.embed_dim)
        self.out_proj = nn.Linear(self.embed_dim, self.embed_dim)

    def forward(
            self,
            layer: int,
            hidden_states: torch.Tensor,
            output_attentions: bool = False,
            past_key_values: torch.Tensor = None,
            current_layer: int = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        """Input shape: Batch x Time x Channel"""

        bsz, tgt_len, embed_dim = hidden_states.size()  # [bs, len, dim]

        # Author----------------------------------------------------------------------------------------------------------
        past_key_values = None
        # --------------------------------------------------------------------------------------------------------------

        # get query proj
        query_states = self.q_proj(hidden_states) * self.scale  # [bs, len, dim]
        # key_states = self._shape(self.k_proj(hidden_states), -1, bsz)
        value_states = self._shape(self.v_proj(hidden_states), -1, bsz)  # [bs, num_heads, len, head_dim]

        # if past_key_values is not None:
        #     key_states = torch.cat([past_key_values[0], key_states], dim=2)
        #     value_states = torch.cat([past_key_values[1], value_states], dim=2)

        proj_shape = (bsz * self.num_heads, -1, self.head_dim)
        query_states = self._shape(query_states, tgt_len, bsz)  # [bs, num_heads, len, head_dim]

        query_states = query_states.view(*proj_shape)  # [bs*num_head, len, head_dim]
        # Author----------------------------------------------------------------------------------------------------------
        # key_states = key_states.view(*proj_shape)                           # [bs*num_head, len, head_dim]
        # --------------------------------------------------------------------------------------------------------------
        value_states = value_states.view(*proj_shape)  # [bs*num_head, len, head_dim]

        # Author
        # src_len = key_states.size(1)
        src_len = value_states.size(1)

        # Author----------------------------------------------------------------------------------------------------------
        if layer < START_SHARE_LAYER:
            key_states = self._shape(self.k_proj(hidden_states), -1, bsz)  # [bs, num_heads, len, head_dim]
            key_states = key_states.view(*proj_shape)  # [bs*num_head, len, head_dim]
            attn_weights = torch.bmm(query_states, key_states.transpose(1, 2))  # [bs*num_head, len, len]
        else:
            attn_weights = ShareKey.cal_attention(query_states, layer=layer, modality='vision', shape=3)
            AttentionReg.attention_vision_list.append(attn_weights)
        # if layer >= _START_SHARE_LAYER:                              # ablation
        #     AttentionReg.attention_vision_list.append(attn_weights)
        # --------------------------------------------------------------------------------------------------------------

        if attn_weights.size() != (bsz * self.num_heads, tgt_len, src_len):
            raise ValueError(
                f"Attention weights should be of size {(bsz * self.num_heads, tgt_len, src_len)}, but is {attn_weights.size()}"
            )
        attn_weights = nn.functional.softmax(attn_weights, dim=-1)

        if output_attentions:
            # this operation is a bit akward, but it's required to
            # make sure that attn_weights keeps its gradient.
            # In order to do so, attn_weights have to reshaped
            # twice and have to be reused in the following
            attn_weights_reshaped = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights_reshaped.view(bsz * self.num_heads, tgt_len, src_len)
        else:
            attn_weights_reshaped = None

        attn_probs = nn.functional.dropout(attn_weights, p=self.dropout, training=self.training)

        attn_output = torch.bmm(attn_probs, value_states)

        if attn_output.size() != (bsz * self.num_heads, tgt_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, tgt_len, self.head_dim)}, but is {attn_output.size()}"
            )

        attn_output = attn_output.view(bsz, self.num_heads, tgt_len, self.head_dim)
        attn_output = attn_output.transpose(1, 2)
        attn_output = attn_output.reshape(bsz, tgt_len, embed_dim)

        attn_output = self.out_proj(attn_output)

        return attn_output, attn_weights_reshaped

    def _shape(self, tensor: torch.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).transpose(1, 2).contiguous()


class CLIPMLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.activation_fn = ACT2FN[config.hidden_act]
        self.fc1 = nn.Linear(config.hidden_size, config.intermediate_size)
        self.fc2 = nn.Linear(config.intermediate_size, config.hidden_size)

    def forward(self, hidden_states):
        hidden_states = self.fc1(hidden_states)
        hidden_states = self.activation_fn(hidden_states)
        hidden_states = self.fc2(hidden_states)
        return hidden_states


class BertSelfAttention(nn.Module):
    MODIFY = False  # Modify Attention Reg

    def __init__(self, config):
        super().__init__()
        self.num_attention_heads = config.num_attention_heads  # 12
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)  # 64
        self.all_head_size = self.num_attention_heads * self.attention_head_size  # 768

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.fusion = BertFusion(config)  #

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
            self,
            layer,
            hidden_states,
            attention_mask=None,
            head_mask=None,
            output_attentions=False,
            visual_hidden_state=None,
            output_qks=None,
            current_layer=None,
    ):
        mixed_query_layer = self.query(hidden_states)

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        # key_layer = self.transpose_for_scores(self.key(hidden_states))          # [bsz, num_heads, len, heads_dim]
        value_layer = self.transpose_for_scores(self.value(hidden_states))  # [bsz, num_heads, len, heads_dim]
        query_layer = self.transpose_for_scores(mixed_query_layer)  # [bsz, num_heads, len, heads_dim]

        # Author----------------------------------------------------------------------------------------------------------
        # qks = (key_layer, value_layer) if output_qks else None
        qks = None
        # --------------------------------------------------------------------------------------------------------------

        # Take the dot product between "query" and "key" to get the raw attention scores.
        # Author----------------------------------------------------------------------------------------------------------
        if layer < START_SHARE_LAYER:
            key_layer = self.transpose_for_scores(self.key(hidden_states))  # [bsz, num_heads, len, heads_dim]
            attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))  # [bsz, num_heads, len, len]
        else:
            attention_scores = ShareKey.cal_attention(query_layer, layer=layer, modality='text',
                                                      shape=4)  # [bsz, num_heads, len, len]
        # --------------------------------------------------------------------------------------------------------------
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask
        # Author---------------------------------------------------------------------------------------------------------
        if layer >= START_SHARE_LAYER:
            if BertSelfAttention.MODIFY:  # Modify
                AttentionReg.attention_text_list.append(
                    torch.where(attention_scores < -9000, AttentionReg.zero, attention_scores))
            else:  # don't modify
                AttentionReg.attention_text_list.append(attention_scores)
        # if layer >= _START_SHARE_LAYER:
        #     AttentionReg.attention_text_list.append(attention_scores)
        # -------------------------------------------------------------------------------------------------------------
        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask
        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)  # bsz, 128, 768

        # Author----------------------------------------------------------------------------------------------------------
        # fusion_output = self.fusion(context_layer, visual_hidden_state, current_layer) if visual_hidden_state is not None else None # add
        fusion_output = None
        # --------------------------------------------------------------------------------------------------------------

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        return outputs, fusion_output, qks


class BertSelfOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertFusion(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.fusion_function = 'softmax'

    def forward(
            self,
            hidden_states,
            visual_hidden_state=None,
            current_layer=None,
    ):
        fusion_scores = torch.matmul(hidden_states, visual_hidden_state.transpose(-1, -2))  # bsz, 128, 49
        if self.fusion_function == 'softmax':
            fusion_probs = nn.Softmax(dim=-1)(fusion_scores)
            fusion_output = torch.matmul(fusion_probs, visual_hidden_state)
        elif self.fusion_function == 'max':
            fusion_probs = fusion_scores.max(dim=-1)
        return fusion_output


class BertAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.self = BertSelfAttention(config)
        self.output = BertSelfOutput(config)
        self.pruned_heads = set()

    def forward(
            self,
            layer,
            hidden_states,
            attention_mask=None,
            head_mask=None,
            output_attentions=False,
            visual_hidden_state=None,
            output_qks=None,
            current_layer=None
    ):
        self_outputs, fusion_output, qks = self.self(
            layer,
            hidden_states,
            attention_mask,
            head_mask,
            output_attentions,
            visual_hidden_state,
            output_qks,
            current_layer
        )
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs, fusion_output, qks


class BertIntermediate(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
        self.fusion_dense = nn.Linear(config.hidden_size, config.intermediate_size)
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states, fusion_output=None):
        hidden_states = self.dense(hidden_states)
        if fusion_output is not None:
            fusion_states = self.fusion_dense(fusion_output)
            hidden_states = hidden_states + fusion_states
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class BertOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class CLIPEncoderLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.embed_dim = config.hidden_size
        self.self_attn = CLIPAttention(config)
        self.layer_norm1 = nn.LayerNorm(self.embed_dim)
        self.mlp = CLIPMLP(config)
        self.layer_norm2 = nn.LayerNorm(self.embed_dim)

    def forward(
            self,
            layer,
            hidden_states: torch.Tensor,
            output_attentions: bool = False,
            past_key_values: torch.Tensor = None,
            current_layer: int = None,
    ):
        """
        Args:
            hidden_states (:obj:`torch.FloatTensor`): input to the layer of shape :obj:`(seq_len, batch, embed_dim)`
            attention_mask (:obj:`torch.FloatTensor`): attention mask of size
                :obj:`(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            layer_head_mask (:obj:`torch.FloatTensor`): mask for attention heads in a given layer of size
                :obj:`(config.encoder_attention_heads,)`.
            output_attentions (:obj:`bool`, `optional`):
                Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under
                returned tensors for more detail.
        """
        residual = hidden_states

        hidden_states = self.layer_norm1(hidden_states)
        hidden_states, attn_weights = self.self_attn(
            layer=layer,
            hidden_states=hidden_states,
            output_attentions=output_attentions,
            past_key_values=past_key_values,
            current_layer=current_layer,
        )
        hidden_states = residual + hidden_states

        residual = hidden_states
        hidden_states = self.layer_norm2(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (attn_weights,)

        return outputs


class BertLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.attention = BertAttention(config)
        self.add_cross_attention = config.add_cross_attention
        self.intermediate = BertIntermediate(config)
        self.output = BertOutput(config)

    def forward(
            self,
            layer,
            hidden_states,
            attention_mask=None,
            head_mask=None,
            output_attentions=False,
            visual_hidden_state=None,
            output_qks=None,
            current_layer=None,
    ):
        # decoder uni-directional self-attention cached key/values tuple is at positions 1,2
        # self_attn_past_key_value = past_key_value[:2] if past_key_value is not None else None

        self_attention_outputs, fusion_output, qks = self.attention(
            layer,
            hidden_states,
            attention_mask,
            head_mask,
            output_attentions=output_attentions,
            visual_hidden_state=visual_hidden_state,
            output_qks=output_qks,
            current_layer=current_layer,
        )
        attention_output = self_attention_outputs[0]

        outputs = self_attention_outputs[1:]  # add self attentions if we output attention weights

        # Author----------------------------------------------------------------------------------------------------------
        layer_output = apply_chunking_to_forward(
            self.feed_forward_chunk, self.chunk_size_feed_forward, self.seq_len_dim, attention_output, None
            # , fusion_output
        )
        # --------------------------------------------------------------------------------------------------------------
        outputs = (layer_output,) + outputs
        if output_qks:
            outputs += (qks,)

        return outputs

    def feed_forward_chunk(self, attention_output, fusion_output):
        intermediate_output = self.intermediate(attention_output, fusion_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output


class UnimoEncoder(nn.Module):
    def __init__(self, vision_config, text_config):
        super().__init__()
        self.vision_config = vision_config
        self.text_config = text_config

        self.vision_layers = nn.ModuleList(
            [CLIPEncoderLayer(vision_config) for _ in range(vision_config.num_hidden_layers)])
        self.text_layer = nn.ModuleList([BertLayer(text_config) for _ in range(text_config.num_hidden_layers)])

    def forward(
            self,
            vision_embeds=None,
            text_embeds=None,
            attention_mask=None,
            head_mask=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=False,
    ):
        assert self.vision_config.num_hidden_layers == self.text_config.num_hidden_layers

        all_vision_hidden_states = () if output_hidden_states else None
        all_text_hidden_states = () if output_hidden_states else None
        all_vision_attentions = () if output_attentions else None
        all_text_attentions = () if output_attentions else None

        vision_hidden_states = vision_embeds
        text_hidden_states = text_embeds
        for idx in range(self.vision_config.num_hidden_layers):
            if output_hidden_states:
                all_vision_hidden_states = all_vision_hidden_states + (vision_hidden_states,)
                all_text_hidden_states = all_text_hidden_states + (text_hidden_states,)

            # vision
            # TODO: 9-12 layers past text as pkv to vision
            # Author----------------------------------------------------------------------------------------------------------
            # past_key_values = text_layer_output[-1] if idx >= 8 else None
            past_key_values = None
            # --------------------------------------------------------------------------------------------------------------
            vision_layer_module = self.vision_layers[idx]
            vision_layer_output = vision_layer_module(
                idx,
                vision_hidden_states,
                output_attentions=output_attentions,
                past_key_values=past_key_values,
                current_layer=idx,
            )
            vision_hidden_states = vision_layer_output[0]

            # text
            # TODO: 9-12 layers past vison qks to text
            # Author----------------------------------------------------------------------------------------------------------
            # last_hidden_state = vision_hidden_states if idx >= 8 else None
            last_hidden_state = None
            # output_qks = True if idx >= 7 else None
            output_qks = None
            # --------------------------------------------------------------------------------------------------------------
            layer_head_mask = head_mask[idx] if head_mask is not None else None
            text_layer_module = self.text_layer[idx]
            text_layer_output = text_layer_module(
                idx,
                text_hidden_states,
                attention_mask=attention_mask,
                head_mask=layer_head_mask,
                visual_hidden_state=last_hidden_state,
                output_attentions=output_attentions,
                output_qks=output_qks,
                current_layer=idx,
            )
            text_hidden_states = text_layer_output[0]
            if output_attentions:
                all_vision_attentions = all_vision_attentions + (vision_layer_output[1],)
                all_text_attentions = all_text_attentions + (text_layer_output[1],)

        if output_hidden_states:
            all_vision_hidden_states = all_vision_hidden_states + (vision_hidden_states,)
            all_text_hidden_states = all_text_hidden_states + (text_hidden_states,)

        if not return_dict:
            return tuple(
                v for v in [
                    text_hidden_states,
                    vision_hidden_states,
                    all_text_hidden_states,
                    all_text_attentions,
                ] if v is not None)

        assert False
        return BaseModelOutput(
            last_hidden_state=text_hidden_states, hidden_states=all_text_hidden_states, attentions=all_text_attentions
        )


class BertPooler(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class UnimoModel(nn.Module):
    def __init__(self, vision_config, text_config, n_class, add_pooling_layer=True):
        super(UnimoModel, self).__init__()
        # vision model
        self.vision_config = vision_config
        self.vision_embeddings = CLIPVisionEmbeddings(vision_config)
        self.vision_pre_layrnorm = nn.LayerNorm(vision_config.hidden_size)
        self.vision_post_layernorm = nn.LayerNorm(vision_config.hidden_size)

        # text model
        self.text_config = text_config
        self.text_embeddings = BertEmbeddings(text_config)
        self.text_pooler = BertPooler(text_config) if add_pooling_layer else None

        self.text_classifier = TextClassifier(text_config.hidden_size, n_class)
        self.vision_classifier = VisionClassifier(vision_config.hidden_size, n_class)
        self.cat_classifier = CatClassifier(text_config.hidden_size + vision_config.hidden_size, n_class)

        # all
        self.encoder = UnimoEncoder(vision_config, text_config)

        self.device = torch.device("cuda")


        # Author----------------------------------------------------------------------------------------------------------
        ShareKey.config = vision_config
        # ShareKey.t_max_l = 80  # text_config.max_position_embeddings
        ShareKey.t_max_l = 128  # text_config.max_position_embeddings
        ShareKey.v_max_l = 61
        ShareKey.layer = 12
        ShareKey.init()
        # --------------------------------------------------------------------------------------------------------------

    def fusion_module(self, text_seq, vision_seq):
        # text_seq: [bs, len1, hidden]
        # vision_seq: [bs, len2, hidden]
        # BertEmbeddings.embedding          # [bs, len1, hidden]
        vision_seq_cls = vision_seq[:,0,:].unsqueeze(1)  # [bs, hidden] --> [bs, 1, hidden]
        vision_seq_in_text = vision_seq_cls + BertEmbeddings.embedding  # [bs, len1, hidden]
        return vision_seq_in_text
        # return torch.cat((text_seq, vision_seq_in_text), dim=-1)        # [bs, len1, hidden*2]

    def forward(
            self,
            input_ids=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,

            pixel_values=None,
            aux_values=None,
            rcnn_values=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=False,  # Author: None->False
    ):
        # pre vision
        vision_embedding_output = self.vision_embeddings(pixel_values, aux_values, rcnn_values)
        vision_embedding_output = self.vision_pre_layrnorm(vision_embedding_output)

        # pre text
        input_shape = input_ids.size()
        batch_size, seq_length = input_shape
        device = input_ids.device
        if attention_mask is None:
            attention_mask = torch.ones(((batch_size, seq_length)), device=device)
        if token_type_ids is None:
            raise ValueError("token_type_ids is None!")

        extended_attention_mask: torch.Tensor = get_extended_attention_mask(attention_mask, input_shape, device)
        head_mask = get_head_mask(head_mask, self.text_config.num_hidden_layers)  # [None]*12

        text_embedding_output = self.text_embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
        )

        # all encoder
        encoder_outputs = self.encoder(
            vision_embeds=vision_embedding_output,
            text_embeds=text_embedding_output,
            attention_mask=extended_attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=False,
        )

        # Author:---------------------------------------------------------------------------------------------------------
        # print(encoder_outputs)
        text_output = encoder_outputs[0]  # sequence embedding
        vision_output = encoder_outputs[1]  # sequence embedding
        mode = ['sep', 'cat'][1]
        if mode == 'sep':
            text_logits = self.text_classifier(text_output)
            vision_logits = self.vision_classifier(vision_output)
        elif mode == 'cat':
            vision_seq_in_text = self.fusion_module(text_seq=text_output, vision_seq=vision_output) # [bs,len1,hidden]
            cat_logits = self.cat_classifier(vision_seq_in_text, text_output)   # [bs, len1, n_class]
            return (text_output, vision_seq_in_text, cat_logits)
        else:
            assert False
        # if not return_dict:
        # print(text_logits)
        return (text_output, vision_output, text_logits, vision_logits)  # + encoder_outputs[2:]

        # sequence_output = encoder_outputs[0]
        # pooled_output = self.text_pooler(sequence_output) if self.text_pooler is not None else None
        #
        # if not return_dict:
        #     return (sequence_output, pooled_output) + encoder_outputs[1:]
        # --------------------------------------------------------------------------------------------------------------


    def _init_text_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, nn.Linear):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.text_config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.text_config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    def get_input_embeddings(self):
        return self.text_embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.text_embeddings.word_embeddings = value

    def resize_token_embeddings(self, new_num_tokens):
        old_embeddings = self.get_input_embeddings()
        new_embeddings = self._get_resized_embeddings(old_embeddings, new_num_tokens)
        self.set_input_embeddings(new_embeddings)

    def _get_resized_embeddings(
            self, old_embeddings: nn.Embedding, new_num_tokens: Optional[int] = None
    ) -> nn.Embedding:
        """
        Build a resized Embedding Module from a provided token Embedding Module. Increasing the size will add newly
        initialized vectors at the end. Reducing the size will remove vectors from the end

        Args:
            old_embeddings (:obj:`torch.nn.Embedding`):
                Old embeddings to be resized.
            new_num_tokens (:obj:`int`, `optional`):
                New number of tokens in the embedding matrix.

                Increasing the size will add newly initialized vectors at the end. Reducing the size will remove
                vectors from the end. If not provided or :obj:`None`, just returns a pointer to the input tokens
                :obj:`torch.nn.Embedding`` module of the model without doing anything.

        Return:
            :obj:`torch.nn.Embedding`: Pointer to the resized Embedding Module or the old Embedding Module if
            :obj:`new_num_tokens` is :obj:`None`
        """
        if new_num_tokens is None:
            return old_embeddings
        else:
            old_num_tokens, old_embedding_dim = old_embeddings.weight.size()

        if old_num_tokens == new_num_tokens:
            return old_embeddings

        if not isinstance(old_embeddings, nn.Embedding):
            raise TypeError(
                f"Old embeddings are of type {type(old_embeddings)}, which is not an instance of {nn.Embedding}."
                f"You should either use a different resize function or make sure that `old_embeddings` are an instance of {nn.Embedding}."
            )

        # Build new embeddings
        new_embeddings = nn.Embedding(new_num_tokens, old_embedding_dim).to(
            self.device, dtype=old_embeddings.weight.dtype
        )

        # initialize all new embeddings (in particular added tokens)
        self._init_text_weights(new_embeddings)

        # Copy token embeddings from the previous weights

        # numbers of tokens to copy
        n = min(old_num_tokens, new_num_tokens)
        new_embeddings.weight.data[:n, :] = old_embeddings.weight.data[:n, :]

        return new_embeddings