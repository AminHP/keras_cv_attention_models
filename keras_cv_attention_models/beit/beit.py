import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import backend as K
from keras_cv_attention_models.attention_layers import (
    BiasLayer,
    ChannelAffine,
    ClassToken,
    conv2d_no_bias,
    drop_block,
    drop_connect_rates_split,
    layer_norm,
)
from keras_cv_attention_models.download_and_load import reload_model_weights_with_mismatch

LAYER_NORM_EPSILON = 1e-6

PRETRAINED_DICT = {
    "beit_base_patch16": {"224": "0a86897f764f9555e44f1dc2a2e9ca87", "384": "58dea9700340ed403c7561b9cab1930f"},
    "beit_large_patch16": {"224": "d5a06dce4ed287f8ca58cdda797e58a9", "384": "ddc46a352d5d9a36ca3f599b9ac0cff2", "512": "42c1bfd8385f8af8b9aa0ddf0e96ed66"},
}


@tf.keras.utils.register_keras_serializable(package="beit")
class MultiHeadRelativePositionalEmbedding(keras.layers.Layer):
    def build(self, attn_shape):
        # print(attn_shape)
        height = width = int(tf.math.sqrt(float(attn_shape[2] - 1)))  # assume hh == ww, e.g. 14
        num_heads = attn_shape[1]
        num_relative_distance = (2 * height - 1) * (2 * width - 1) + 3
        self.relative_position_bias_table = self.add_weight(name="pos_emb", shape=(num_relative_distance, num_heads), initializer="zeros", trainable=True)

        xx, yy = tf.meshgrid(range(height), range(width))  # tf.meshgrid is same with np.meshgrid 'xy' mode, while torch.meshgrid 'ij' mode
        coords = tf.stack([yy, xx], axis=-1)  # [14, 14, 2]
        coords_flatten = tf.reshape(coords, [-1, 2])  # [196, 2]
        relative_coords = coords_flatten[:, None, :] - coords_flatten[None, :, :]  # [196, 196, 2]
        xx = (relative_coords[:, :, 0] + height - 1) * (2 * width - 1)
        yy = relative_coords[:, :, 1] + width - 1
        relative_coords = tf.stack([xx, yy], axis=-1)

        relative_position_index = tf.reduce_sum(relative_coords, axis=-1)  # [196, 196]
        top = tf.ones((1, relative_position_index.shape[1]), dtype=relative_position_index.dtype) * (num_relative_distance - 3)
        left = tf.ones((relative_position_index.shape[0], 1), dtype=relative_position_index.dtype) * (num_relative_distance - 2)
        corner = tf.ones((1, 1), dtype=relative_position_index.dtype) * (num_relative_distance - 1)
        # print(f">>>> {top.shape = }, {left.shape = }, {corner.shape = }")
        # >>>> top.shape = TensorShape([1, 196]), left.shape = TensorShape([196, 1]), corner.shape = TensorShape([1, 1])
        left_corner = tf.concat([corner, left], axis=0)
        relative_position_index = tf.concat([top, relative_position_index], axis=0)
        self.relative_position_index = tf.concat([left_corner, relative_position_index], axis=1)  # [197, 197]

    def call(self, attention_scores, **kwargs):
        pos_emb = tf.gather(self.relative_position_bias_table, self.relative_position_index)
        pos_emb = tf.transpose(pos_emb, [2, 0, 1])
        return attention_scores + pos_emb

    def load_resized_pos_emb(self, source_layer):
        hh = ww = int(tf.math.sqrt(float(source_layer.relative_position_bias_table.shape[0] - 3)))
        num_heads = source_layer.relative_position_bias_table.shape[-1]
        ss = tf.reshape(source_layer.relative_position_bias_table[:-3], (hh, ww, num_heads))  # [hh, ww, num_heads]
        target_hh = target_ww = int(tf.math.sqrt(float(self.relative_position_bias_table.shape[0] - 3)))
        tt = tf.image.resize(ss, [target_hh, target_ww])  # [target_hh, target_ww, num_heads]
        tt = tf.reshape(tt, (tt.shape[0] * tt.shape[1], num_heads))  # [target_hh * target_ww, num_heads]
        tt = tf.concat([tt, source_layer.relative_position_bias_table[-3:]], axis=0)
        self.relative_position_bias_table.assign(tt)


def attention_block(inputs, num_heads=4, key_dim=0, out_weight=True, out_bias=False, qv_bias=True, attn_dropout=0, name=None):
    _, bb, cc = inputs.shape
    key_dim = key_dim if key_dim > 0 else cc // num_heads
    qk_scale = 1.0 / tf.math.sqrt(tf.cast(key_dim, inputs.dtype))
    emded_dim = num_heads * key_dim

    qkv = keras.layers.Dense(emded_dim * 3, use_bias=False, name=name and name + "qkv")(inputs)
    qkv = tf.reshape(qkv, [-1, bb, qkv.shape[-1]])
    query, key, value = tf.split(qkv, 3, axis=-1)
    # query = [batch, num_heads, hh * ww, key_dim]
    if qv_bias:
        query = BiasLayer(name=name + "query_bias")(query)
    query = tf.reshape(query, [-1, query.shape[1], num_heads, key_dim])
    query = tf.transpose(query, [0, 2, 1, 3])
    # key = [batch, num_heads, key_dim, hh * ww]
    key = tf.transpose(tf.reshape(key, [-1, key.shape[1], num_heads, key_dim]), [0, 2, 3, 1])
    # value = [batch, num_heads, hh * ww, key_dim]
    if qv_bias:
        value = BiasLayer(name=name + "value_bias")(value)
    value = tf.reshape(value, [-1, value.shape[1], num_heads, key_dim])
    value = tf.transpose(value, [0, 2, 1, 3])

    query *= qk_scale
    # [batch, num_heads, hh * ww, hh * ww]
    attention_scores = keras.layers.Lambda(lambda xx: tf.matmul(xx[0], xx[1]))([query, key])
    attention_scores = MultiHeadRelativePositionalEmbedding(name=name and name + "pos_emb")(attention_scores)
    attention_scores = tf.nn.softmax(attention_scores, axis=-1)

    if attn_dropout > 0:
        attention_scores = keras.layers.Dropout(attn_dropout, name=name and name + "attn_drop")(attention_scores)
    # value = [batch, num_heads, hh * ww, key_dim]
    # attention_output = tf.matmul(attention_scores, value)  # [batch, num_heads, hh * ww, key_dim]
    attention_output = keras.layers.Lambda(lambda xx: tf.matmul(xx[0], xx[1]))([attention_scores, value])
    attention_output = tf.transpose(attention_output, perm=[0, 2, 1, 3])
    attention_output = tf.reshape(attention_output, [-1, bb, emded_dim])
    # print(f">>>> {attention_output.shape = }, {attention_scores.shape = }")

    if out_weight:
        # [batch, hh, ww, num_heads * key_dim] * [num_heads * key_dim, out] --> [batch, hh, ww, out]
        attention_output = keras.layers.Dense(emded_dim, use_bias=out_bias, name=name and name + "output")(attention_output)
    return attention_output


def attention_mlp_block(inputs, embed_dim, gamma_init_value=0.1, mlp_ratio=4, drop_rate=0, activation="gelu", attn_params={}, name=""):
    # print(f">>>> {drop_rate = }")
    nn = layer_norm(inputs, epsilon=LAYER_NORM_EPSILON, name=name + "attn_")
    nn = attention_block(nn, **attn_params, name=name + "attn_")
    nn = ChannelAffine(use_bias=False, weight_init_value=gamma_init_value, name=name + "attn_gamma")(nn)
    nn = drop_block(nn, drop_rate)
    attn_out = keras.layers.Add(name=name + "attn_out")([inputs, nn])

    """ MLP """
    nn = layer_norm(attn_out, epsilon=LAYER_NORM_EPSILON, name=name + "mlp_")
    nn = keras.layers.Dense(embed_dim * mlp_ratio, name=name + "mlp_dense_1")(nn)
    nn = keras.layers.Activation(activation, name=name + "mlp_" + activation)(nn)
    nn = keras.layers.Dense(embed_dim, name=name + "mlp_dense_2")(nn)
    nn = ChannelAffine(use_bias=False, weight_init_value=gamma_init_value, name=name + "mlp_gamma")(nn)
    nn = drop_block(nn, drop_rate)
    nn = keras.layers.Add(name=name + "mlp_out")([attn_out, nn])
    return nn


@tf.keras.utils.register_keras_serializable(package="beit")
def head_init(shape, dtype="float32"):
    return tf.initializers.TruncatedNormal(stddev=0.02)(shape, dtype=dtype) * 0.001


def Beit(
    depth=12,
    embed_dim=768,
    num_heads=12,
    mlp_ratio=4,
    patch_size=16,
    attn_key_dim=0,
    attn_qv_bias=True,
    attn_out_weight=True,
    attn_out_bias=True,
    attn_dropout=0,
    gamma_init_value=0.1,
    input_shape=(224, 224, 3),
    num_classes=1000,
    activation="gelu",
    drop_connect_rate=0,
    use_mean_pooling=True,
    classifier_activation="softmax",
    pretrained="imagenet",
    model_name="beit",
    kwargs=None,
):
    inputs = keras.layers.Input(input_shape)

    """ forward_embeddings """
    nn = conv2d_no_bias(inputs, embed_dim, patch_size, strides=patch_size, padding="valid", use_bias=True, name="stem_")
    nn = keras.layers.Reshape([-1, nn.shape[-1]])(nn)
    nn = ClassToken(name="cls_token")(nn)

    attn_params = {
        "num_heads": num_heads,
        "key_dim": attn_key_dim,
        "qv_bias": attn_qv_bias,
        "out_weight": attn_out_weight,
        "out_bias": attn_out_bias,
        "attn_dropout": attn_dropout,
    }

    """ forward_tokens """
    drop_connect_rates = drop_connect_rates_split([depth], 0.0, drop_connect_rate)[0]
    for id in range(depth):
        name = "block{}_".format(id)
        block_drop_rate = drop_connect_rates[id]
        nn = attention_mlp_block(nn, embed_dim, gamma_init_value, mlp_ratio, block_drop_rate, activation, attn_params, name=name)

    if use_mean_pooling:
        nn = tf.reduce_mean(nn[:, 1:, :], axis=1)
        nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, name="out_")
    else:
        nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, name="out_")[:, 0]

    if num_classes > 0:
        nn = keras.layers.Dense(
            num_classes, dtype="float32", activation=classifier_activation, kernel_initializer=head_init, bias_initializer=head_init, name="predictions"
        )(nn)
    model = tf.keras.models.Model(inputs, nn, name=model_name)

    """ Reload model weights by input_shape """
    pre_resolutions = PRETRAINED_DICT[model.name]
    max_resolution = max([int(ii) for ii in pre_resolutions.keys()])
    request_resolution = input_shape[0] if str(input_shape[0]) in pre_resolutions else max_resolution
    pretrained = str(request_resolution) if pretrained is not None else None
    reload_model_weights_with_mismatch(model, PRETRAINED_DICT, "beit", MultiHeadRelativePositionalEmbedding, request_resolution, input_shape, pretrained)
    return model


def BeitBasePatch16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    embed_dim = 768
    depth = 12
    num_heads = 12
    gamma_init_value = 0.1
    return Beit(**locals(), model_name="beit_base_patch16", **kwargs)


def BeitLargePatch16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    embed_dim = 1024
    depth = 24
    num_heads = 16
    gamma_init_value = 1e-5
    return Beit(**locals(), model_name="beit_large_patch16", **kwargs)