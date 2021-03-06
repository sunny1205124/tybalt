"""
tybalt/models.py
2017 Gregory Way

Functions enabling the construction and usage of Tybalt and ADAGE models
"""

import numpy as np
import pandas as pd

from keras import backend as K
from keras import optimizers
from keras.layers import Input, Dense, Lambda, Activation, Dropout
from keras.layers.normalization import BatchNormalization
from keras.layers.merge import concatenate
from keras.models import Model, Sequential
from keras.regularizers import l1

from tybalt.utils.vae_utils import VariationalLayer, WarmUpCallback
from tybalt.utils.vae_utils import LossCallback
from tybalt.utils.adage_utils import TiedWeightsDecoder
from tybalt.utils.base import VAE, BaseModel


class Tybalt(VAE):
    """
    Training and evaluation of a tybalt model

    Usage: from tybalt.models import Tybalt
    """
    def __init__(self, original_dim, latent_dim, batch_size=50, epochs=50,
                 learning_rate=0.0005, kappa=1, epsilon_std=1.0,
                 beta=K.variable(0), loss='binary_crossentropy',
                 verbose=True):
        VAE.__init__(self)
        self.model_name = 'Tybalt'
        self.original_dim = original_dim
        self.latent_dim = latent_dim
        self.batch_size = batch_size
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.kappa = kappa
        self.epsilon_std = epsilon_std
        self.beta = beta
        self.loss = loss
        self.verbose = verbose

    def _build_encoder_layer(self):
        """
        Function to build the encoder layer connections
        """
        # Input place holder for RNAseq data with specific input size
        self.rnaseq_input = Input(shape=(self.original_dim, ))

        # Input layer is compressed into a mean and log variance vector of
        # size `latent_dim`. Each layer is initialized with glorot uniform
        # weights and each step (dense connections, batch norm, and relu
        # activation) are funneled separately.
        # Each vector are connected to the rnaseq input tensor

        # input layer to latent mean layer
        z_mean = Dense(self.latent_dim,
                       kernel_initializer='glorot_uniform')(self.rnaseq_input)
        z_mean_batchnorm = BatchNormalization()(z_mean)
        self.z_mean_encoded = Activation('relu')(z_mean_batchnorm)

        # input layer to latent standard deviation layer
        z_var = Dense(self.latent_dim,
                      kernel_initializer='glorot_uniform')(self.rnaseq_input)
        z_var_batchnorm = BatchNormalization()(z_var)
        self.z_var_encoded = Activation('relu')(z_var_batchnorm)

        # return the encoded and randomly sampled z vector
        # Takes two keras layers as input to the custom sampling function layer
        self.z = Lambda(self._sampling,
                        output_shape=(self.latent_dim, ))([self.z_mean_encoded,
                                                           self.z_var_encoded])

    def _build_decoder_layer(self):
        """
        Function to build the decoder layer connections
        """
        # The decoding layer is much simpler with a single layer glorot uniform
        # initialized and sigmoid activation
        self.decoder_model = Sequential()
        self.decoder_model.add(Dense(self.original_dim, activation='sigmoid',
                                     input_dim=self.latent_dim))
        self.rnaseq_reconstruct = self.decoder_model(self.z)

    def _compile_vae(self):
        """
        Creates the vae layer and compiles all layer connections
        """
        adam = optimizers.Adam(lr=self.learning_rate)
        vae_layer = VariationalLayer(var_layer=self.z_var_encoded,
                                     mean_layer=self.z_mean_encoded,
                                     original_dim=self.original_dim,
                                     beta=self.beta, loss=self.loss)(
                                [self.rnaseq_input, self.rnaseq_reconstruct])
        self.full_model = Model(self.rnaseq_input, vae_layer)
        self.full_model.compile(optimizer=adam, loss=None,
                                loss_weights=[self.beta])

    def _connect_layers(self):
        """
        Make connections between layers to build separate encoder and decoder
        """
        self.encoder = Model(self.rnaseq_input, self.z_mean_encoded)

        decoder_input = Input(shape=(self.latent_dim, ))
        _x_decoded_mean = self.decoder_model(decoder_input)
        self.decoder = Model(decoder_input, _x_decoded_mean)

    def train_vae(self, train_df, test_df, separate_loss=False):
        """
        Method to train model.

        `separate_loss` instantiates a custom Keras callback that tracks the
        separate contribution of reconstruction and KL divergence loss. Because
        VAEs try to minimize both, it may be informative to track each across
        training separately. The callback processes the training data through
        the current encoder and decoder and therefore requires additional time
        - which is why this is not done by default.
        """
        cbks = [WarmUpCallback(self.beta, self.kappa)]
        if separate_loss:
            tybalt_loss_cbk = LossCallback(training_data=np.array(train_df),
                                           encoder_cbk=self.encoder,
                                           decoder_cbk=self.decoder,
                                           original_dim=self.original_dim)
            cbks += [tybalt_loss_cbk]

        self.hist = self.full_model.fit(np.array(train_df),
                                        shuffle=True,
                                        epochs=self.epochs,
                                        batch_size=self.batch_size,
                                        verbose=self.verbose,
                                        validation_data=(np.array(test_df),
                                                         None),
                                        callbacks=cbks)
        self.history_df = pd.DataFrame(self.hist.history)

        if separate_loss:
            self.history_df = self.history_df.assign(
                                recon=tybalt_loss_cbk.xent_loss)
            self.history_df = self.history_df.assign(
                                kl=tybalt_loss_cbk.kl_loss)


class cTybalt(VAE):
    """
    Training and evaluation of a cTybalt model (conditional VAE)

    Modified from:
    https://wiseodd.github.io/techblog/2016/12/17/conditional-vae/

    Usage: from tybalt.models import cTybalt

    cTybalt API:

    ctybalt_model = cTybalt(<args>)
    ctybalt_model.initialize_model()
    ctybalt_model.get_summary()
    ctybalt_model.visualize_architecture('example_output_plot.png')
    ctybalt_model.train_cvae(train_df, y_train, test_df, y_test)
    ctybalt_model.visualize_training('training_curves.png')
    ctybalt_model.connect_layers()
    ctybalt_model.compress([new_data_df, new_data_y])
    ctybalt_model.get_decoder_weights()
    ctybalt_model.save_models()
    """
    def __init__(self, original_dim, latent_dim, label_dim,
                 batch_size=50, epochs=50, learning_rate=0.0005, kappa=1,
                 epsilon_std=1.0, beta=K.variable(0),
                 loss='binary_crossentropy', verbose=True):
        VAE.__init__(self)
        self.model_name = 'cTybalt'
        self.original_dim = original_dim
        self.latent_dim = latent_dim
        self.label_dim = label_dim
        self.batch_size = batch_size
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.kappa = kappa
        self.epsilon_std = epsilon_std
        self.beta = beta
        self.loss = loss
        self.verbose = verbose

    def _build_encoder_layer(self):
        """
        Function to build the encoder layer connections for conditional VAE
        """
        # Input place holder for RNAseq and label data with specific input size
        self.rnaseq_input = Input(shape=(self.original_dim, ))
        self.label_input = Input(shape=(self.label_dim, ))

        # Concatenate input layers to obtain single input into model
        self.cvae_input = concatenate([self.rnaseq_input, self.label_input])

        # Input layer is compressed into a mean and log variance vector of
        # size `latent_dim`. Each layer is initialized with glorot uniform
        # weights and each step (dense connections, batch norm, and relu
        # activation) are funneled separately.
        # Each vector are connected to the rnaseq input and label tensors

        # input layer to latent mean layer
        z_mean = Dense(self.latent_dim,
                       kernel_initializer='glorot_uniform')(self.cvae_input)
        z_mean_batchnorm = BatchNormalization()(z_mean)
        self.z_mean_encoded = Activation('relu')(z_mean_batchnorm)

        # input layer to latent standard deviation layer
        z_var = Dense(self.latent_dim,
                      kernel_initializer='glorot_uniform')(self.cvae_input)
        z_var_batchnorm = BatchNormalization()(z_var)
        self.z_var_encoded = Activation('relu')(z_var_batchnorm)

        # return the encoded and randomly sampled z vector
        # Takes two keras layers as input to the custom sampling function layer
        self.z = Lambda(self._sampling,
                        output_shape=(self.latent_dim, ))([self.z_mean_encoded,
                                                           self.z_var_encoded])

        # To make the model conditional, add back the label layer to the latent
        # features. This will encourage the z vector to learn common sources of
        # variation, while the label input layer can extract variation
        # conditioned on the specific sample labels.
        self.zc = concatenate([self.z, self.label_input])

    def _build_decoder_layer(self):
        """
        Function to build the decoder layer connections for conditional VAE
        """
        # The decoding layer is much simpler with a single layer glorot uniform
        # initialized and sigmoid activation

        self.cvae_input_dim = self.original_dim + self.label_dim
        self.cvae_latent_dim = self.latent_dim + self.label_dim

        self.decoder_model = Sequential()
        self.decoder_model.add(Dense(self.cvae_input_dim, activation='sigmoid',
                                     input_dim=self.cvae_latent_dim))
        self.rnaseq_reconstruct = self.decoder_model(self.zc)

    def _compile_vae(self):
        """
        Creates the vae layer and compiles all layer connections
        """
        adam = optimizers.Adam(lr=self.learning_rate)
        cvae_layer = VariationalLayer(var_layer=self.z_var_encoded,
                                      mean_layer=self.z_mean_encoded,
                                      original_dim=self.original_dim,
                                      beta=self.beta, loss=self.loss)(
                                [self.cvae_input, self.rnaseq_reconstruct])
        self.full_model = Model([self.rnaseq_input, self.label_input],
                                cvae_layer)
        self.full_model.compile(optimizer=adam, loss=None,
                                loss_weights=[self.beta])

    def _connect_layers(self):
        # Make connections between layers to build separate encoder and decoder
        self.encoder = Model([self.rnaseq_input, self.label_input],
                             self.z_mean_encoded)

        decoder_input = Input(shape=(self.cvae_latent_dim, ))
        _x_decoded_mean = self.decoder_model(decoder_input)
        self.decoder = Model(decoder_input, _x_decoded_mean)

    def train_cvae(self, train_df, train_labels_df, test_df, test_labels_df):
        train_input = [np.array(train_df), np.array(train_labels_df)]
        val_input = ([np.array(test_df), np.array(test_labels_df)], None)
        self.hist = self.full_model.fit(train_input,
                                        shuffle=True,
                                        epochs=self.epochs,
                                        verbose=self.verbose,
                                        batch_size=self.batch_size,
                                        validation_data=val_input,
                                        callbacks=[WarmUpCallback(self.beta,
                                                                  self.kappa)])
        self.history_df = pd.DataFrame(self.hist.history)


class Adage(BaseModel):
    """
    Training and evaluation of an ADAGE model

    Usage: from tybalt.models import Adage
    """
    def __init__(self, original_dim, latent_dim, noise=0.05, batch_size=50,
                 epochs=100, sparsity=0, learning_rate=0.0005, loss='mse',
                 optimizer='adam', tied_weights=True, verbose=True):
        BaseModel.__init__(self)
        self.model_name = 'ADAGE'
        self.original_dim = original_dim
        self.latent_dim = latent_dim
        self.noise = noise
        self.batch_size = batch_size
        self.epochs = epochs
        self.sparsity = sparsity
        self.learning_rate = learning_rate
        self.loss = loss
        self.optimizer = optimizer
        self.tied_weights = tied_weights
        self.verbose = verbose

    def _build_graph(self):
        # Build the Keras graph for an ADAGE model
        self.input_rnaseq = Input(shape=(self.original_dim, ))
        drop = Dropout(self.noise)(self.input_rnaseq)
        self.encoded = Dense(self.latent_dim,
                             activity_regularizer=l1(self.sparsity))(drop)
        activation = Activation('relu')(self.encoded)
        decoded_rnaseq = Dense(self.original_dim,
                               activation='sigmoid')(activation)

        self.full_model = Model(self.input_rnaseq, decoded_rnaseq)

    def _build_tied_weights_graph(self):
        # Build Keras graph for an ADAGE model with tied weights
        self.encoded = Dense(self.latent_dim,
                             input_shape=(self.original_dim, ),
                             activity_regularizer=l1(self.sparsity),
                             activation='relu')
        dropout_layer = Dropout(self.noise)
        self.tied_decoder = TiedWeightsDecoder(input_shape=(self.latent_dim, ),
                                               output_dim=self.original_dim,
                                               activation='sigmoid',
                                               encoder=self.encoded)
        self.full_model = Sequential()
        self.full_model.add(self.encoded)
        self.full_model.add(dropout_layer)
        self.full_model.add(self.tied_decoder)

    def _compile_adage(self):
        # Compile the autoencoder to prepare for training
        if self.optimizer == 'adadelta':
            optim = optimizers.Adadelta(lr=self.learning_rate)
        elif self.optimizer == 'adam':
            optim = optimizers.Adam(lr=self.learning_rate)
        self.full_model.compile(optimizer=optim, loss=self.loss)

    def _connect_layers(self):
        # Separate out the encoder and decoder model
        encoded_input = Input(shape=(self.latent_dim, ))
        decoder_layer = self.full_model.layers[-1]
        self.decoder = Model(encoded_input, decoder_layer(encoded_input))

        if self.tied_weights:
            # The keras graph is built differently for a tied weight model
            # Build a model with input and output Tensors of the encoded layer
            self.encoder = Model(self.encoded.input, self.encoded.output)
        else:
            self.encoder = Model(self.input_rnaseq, self.encoded)

    def initialize_model(self):
        """
        Helper function to run that builds and compiles Keras layers
        """
        if self.tied_weights:
            self._build_tied_weights_graph()
        else:
            self._build_graph()
        self._connect_layers()
        self._compile_adage()

    def train_adage(self, train_df, test_df, adage_comparable_loss=False):
        self.hist = self.full_model.fit(np.array(train_df), np.array(train_df),
                                        shuffle=True,
                                        epochs=self.epochs,
                                        verbose=self.verbose,
                                        batch_size=self.batch_size,
                                        validation_data=(np.array(test_df),
                                                         np.array(test_df)))
        self.history_df = pd.DataFrame(self.hist.history)

        # ADAGE loss is a mean over all features - to make this value more
        # comparable to the VAE reconstruciton loss, multiply by num genes
        if adage_comparable_loss:
            self.history_df = self.history_df * self.original_dim

    def compress(self, df):
        # Encode rnaseq into the hidden/latent representation - and save output
        encoded_df = self.encoder.predict(np.array(df))
        encoded_df = pd.DataFrame(encoded_df, index=df.index,
                                  columns=range(1, self.latent_dim + 1))
        return encoded_df
