import tensorflow as tf
import tensorflow.keras
from tensorflow.keras import models
from tensorflow.keras.layers import Dense
from tensorflow.keras.layers import Dropout
from tensorflow.keras.layers import Flatten
from tensorflow.keras.layers import Lambda
from tensorflow.keras.layers import TimeDistributed
from tensorflow.keras import backend as K
from . import roi_pooling

class DN(tf.keras.Model):
    def __init__(self, n_of_classes, actclassoutputs, l2, dropout_prob):
        # custom_roi_pool is a flag indicating whether to use a custom roi pooling layer or not
        super().__init__()
        self._num_classes = n_of_classes
        self._activate_class_outputs = actclassoutputs
        self._dropout_probability = dropout_prob
        self._roi_pool = False
        regularizer = tf.keras.regularizers.l2(l2)
        class_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=0.01)
        regressor_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=0.001)

        #check      self._roi_pool = roi_pooling(pool_size = 7, name = "custom_roi_pool") if custom_roi_pool else None

        self._flatten = TimeDistributed(Flatten())#Used to flatten the input at each TimeStep, TimeDistributed used to iterate over each proposal independently
        self._fc1 = TimeDistributed(name = "fc1", layer = Dense(units = 4096, activation = "relu", kernel_regularizer = regularizer))#capture high level features
        self._dropout1 = TimeDistributed(Dropout(dropout_prob))#dropout is appiled to prevent overfitting
        self._fc2 = TimeDistributed(name = "fc2", layer = Dense(units = 4096, activation = "relu", kernel_regularizer = regularizer))#same use case as fc1
        self._dropout2 = TimeDistributed(Dropout(dropout_prob))#same use case as of prev dropout layer

        #output layer
        class_activation = "softmax" if actclassoutputs else None
        self._classifier = TimeDistributed(name = "classifier_class", layer = Dense(units = n_of_classes, activation = class_activation, kernel_initializer = class_initializer))
        self._regressor = TimeDistributed(name = "classifier_boxes", layer = Dense(units = 4 * (n_of_classes - 1), activation = "linear", kernel_initializer = regressor_initializer))#-1 is done to exclude the background class


    def call(self,inp,train):
        input_image=inp[0]
        feature_map=inp[1]
        proposals=inp[2]
        assert len(feature_map.shape)==4

        if self._roi_pool:     #convert the proposals from (y1, x1, y2, x2) to (y1, x1, height, width)
            proposals = tf.cast(proposals, dtype = tf.int32)                 
            map_dimensions = tf.shape(feature_map)[1:3]                      
            map_limits = tf.tile(map_dimensions, multiples = [2]) - 1        
            roi_corners = tf.minimum(proposals // 16, map_limits)            
            roi_corners = tf.maximum(roi_corners, 0)
            roi_dimensions = roi_corners[:,2:4] - roi_corners[:,0:2] + 1
            rois = tf.concat([ roi_corners[:,0:2], roi_dimensions ], axis = 1) 
            rois = tf.expand_dims(rois, axis = 0)                             
            pool = roi_pooling(pool_size = 7, name = "roi_pool")([feature_map, rois])
        else:
            image_height = tf.shape(input_image)[1] 
            image_width = tf.shape(input_image)[2]  
            rois = proposals / [ image_height, image_width, image_height, image_width ]
            #converts the coordinates of proposals to be within the range [0, 1]
            num_rois = tf.shape(rois)[0]
            region = tf.image.crop_and_resize(image = feature_map, boxes = rois, box_indices = tf.zeros(num_rois, dtype = tf.int32), crop_size = [14, 14])#creates roi of 14*14 size
            pool = tf.nn.max_pool(region, ksize = [1, 2, 2, 1], strides = [1, 2, 2, 1], padding = "SAME")#size=7*7
            pool = tf.expand_dims(pool, axis = 0)#Add an extra dimension at the beginning
        
        flattened = self._flatten(pool)
        if train and self._dropout_probability != 0:
            fc1 = self._fc1(flattened)
            do1 = self._dropout1(fc1)
            fc2 = self._fc2(do1)
            do2 = self._dropout2(fc2)
            out = do2
        else:
            fc1 = self._fc1(flattened)
            fc2 = self._fc2(fc1)
            out = fc2 
        class_activation = "softmax" if self._activate_class_outputs else None
        classes = self._classifier(out)
        box_deltas = self._regressor(out)

        return [ classes, box_deltas ]

    @staticmethod
    def class_loss(y_pred, y_true, f_logits):
        scale_factor = 1.0
        N = tf.cast(tf.shape(y_true)[1], dtype = tf.float32) + tf.constant(1e-3)# N=number of proposals  #1e-3 added to avoid division by zero
        if f_logits:
            return scale_factor * K.sum(K.categorical_crossentropy(target = y_true, output = y_pred, from_logits = True)) / N
        else:
            return scale_factor *K.sum(K.categorical_crossentropy(y_true, y_pred)) / N

    @staticmethod
    def regression_loss(y_pred, y_true):
    
        scale_factor = 1.0
        sigma = 1.0
        sigma_squared = sigma * sigma
        y_mask = y_true[:,:,0,:]#Extracts masks[0] from y_true indicating which of the reg_targets to use for each proposal
        y_true_targets = y_true[:,:,1,:]#Extracts the actual reg_targets[1] from y_true
        x = y_true_targets - y_pred
        x_abs = tf.math.abs(x)
        is_negative_branch = tf.stop_gradient(tf.cast(tf.less(x_abs, 1.0 / sigma_squared), dtype = tf.float32))
        R_negative_branch = 0.5 * x * x * sigma_squared #loss for the negative branch.
        R_positive_branch = x_abs - 0.5 / sigma_squared #loss for the positive branch
        losses = is_negative_branch * R_negative_branch + (1.0 - is_negative_branch) * R_positive_branch #Combined losses from the neg and pos branches

        N = tf.cast(tf.shape(y_true)[1], dtype = tf.float32) +  K.epsilon() 
        relevant_loss_terms = y_mask * losses
        return scale_factor * tf.reduce_sum(relevant_loss_terms) / N #scaled by the scale_factor and divided by N