import objax
import jax.numpy as np
from .kernels import Independent
from jax import vmap
from jax.lax import scan
from jax.ops import index, index_update
from jax.scipy.linalg import cho_factor, cho_solve
from jax.lib import xla_bridge
from .utils import (
    inv,
    inv_vmap,
    diag,
    solve,
    transpose,
    input_admin,
    compute_conditional_statistics,
    build_joint,
    set_z_stats,
    temporal_conditional,
    sum_natural_params_by_group,
    gaussian_expected_log_lik,
    compute_cavity
)
from .ops import (
    gaussian_conditional,
    sparse_gaussian_conditional,
    sparse_conditional_post_to_data,
    kalman_filter,
    kalman_filter_pairs,
    rauch_tung_striebel_smoother,
    process_noise_covariance
)
import math
from jax.config import config
config.update("jax_enable_x64", True)

LOG2PI = math.log(2 * math.pi)


class GaussianDistribution(objax.Module):
    """
    A small class defined to handle the fact that we often need access to both the mean / cov parameterisation
    of a Gaussian and its natural parameterisation.
    Important note: for simplicity we let nat2 = inv(cov) rather than nat2 = -0.5inv(cov). The latter is the proper
    natural parameter, but for Gaussian distributions we need not worry about the -0.5 (it cancels out anyway).
    """

    def __init__(self, mean, covariance):
        self.mean_ = objax.StateVar(mean)
        self.covariance_ = objax.StateVar(covariance)
        nat1, nat2 = self.reparametrise(mean, covariance)
        self.nat1_, self.nat2_ = objax.StateVar(nat1), objax.StateVar(nat2)

    def __call__(self):
        return self.mean, self.covariance

    @property
    def mean(self):
        return self.mean_.value

    @property
    def covariance(self):
        return self.covariance_.value

    @property
    def nat1(self):
        return self.nat1_.value

    @property
    def nat2(self):
        return self.nat2_.value

    @staticmethod
    def reparametrise(param1, param2):
        chol = cho_factor(param2)
        reparam1 = cho_solve(chol, param1)
        reparam2 = cho_solve(chol, np.tile(np.eye(param2.shape[1]), [param2.shape[0], 1, 1]))
        return reparam1, reparam2

    def update_mean_cov(self, mean, covariance):
        self.mean_.value = mean
        self.covariance_.value = covariance
        self.nat1_.value, self.nat2_.value = self.reparametrise(mean, covariance)

    def update_nat_params(self, nat1, nat2):
        self.nat1_.value = nat1
        self.nat2_.value = nat2
        self.mean_.value, self.covariance_.value = self.reparametrise(nat1, nat2)


class BaseModel(objax.Module):
    """
    The parent model class: initialises all the common model features and implements shared methods
    TODO: move as much of the generic functionality as possible from this class to the inference class.
    """
    def __init__(self,
                 kernel,
                 likelihood,
                 X,
                 Y,
                 func_dim=1):
        super().__init__()
        if X.ndim < 2:
            X = X[:, None]
        if Y.ndim < 2:
            Y = Y[:, None]
        self.X = np.array(X)
        self.Y = np.array(Y)
        self.kernel = kernel
        self.likelihood = likelihood
        self.num_data = self.X.shape[0]  # number of data
        self.func_dim = func_dim  # number of latent dimensions
        self.obs_dim = Y.shape[1]  # dimensionality of the observations, Y
        if isinstance(self.kernel, Independent):
            pseudo_lik_size = self.func_dim  # the multi-latent case
        else:
            pseudo_lik_size = self.obs_dim
        self.pseudo_likelihood = GaussianDistribution(
            mean=np.zeros([self.num_data, pseudo_lik_size, 1]),
            covariance=1e2 * np.tile(np.eye(pseudo_lik_size), [self.num_data, 1, 1])
        )
        self.posterior_mean = objax.StateVar(np.zeros([self.num_data, self.func_dim, 1]))
        self.posterior_variance = objax.StateVar(np.tile(np.eye(self.func_dim), [self.num_data, 1, 1]))
        self.ind = np.arange(self.num_data)
        self.num_neighbours = np.ones(self.num_data)
        self.mask_y = np.isnan(self.Y).reshape(Y.shape[0], Y.shape[1])
        if self.func_dim != self.obs_dim:
            self.mask_pseudo_y = np.tile(self.mask_y, [1, pseudo_lik_size])  # multi-latent case
        else:
            self.mask_pseudo_y = self.mask_y

    def __call__(self, X=None):
        if X is None:
            self.update_posterior()
        else:
            return self.predict(X)

    def prior_sample(self, num_samps=1, X=None, seed=0):
        raise NotImplementedError

    def update_posterior(self):
        raise NotImplementedError

    def compute_log_lik(self, pseudo_y=None, pseudo_var=None):
        """ Compute the log likelihood of the pseudo model, i.e. the log normaliser of the approximate posterior """
        raise NotImplementedError

    def predict(self, X, R=None):
        raise NotImplementedError

    def predict_y(self, X, R=None, cubature=None):
        """
        predict y at new test locations X
        TODO: check non-Gaussian likelihoods
        """
        mean_f, var_f = self.predict(X, R)
        mean_f, var_f = mean_f.reshape(mean_f.shape[0], -1, 1), var_f.reshape(var_f.shape[0], -1, 1)
        mean_y, var_y = vmap(self.likelihood.predict, (0, 0, None))(mean_f, var_f, cubature)
        return np.squeeze(mean_y), np.squeeze(var_y)

    def negative_log_predictive_density(self, X, Y, R=None, cubature=None):
        predict_mean, predict_var = self.predict(X, R)
        if Y.ndim < 2:
            Y = Y.reshape(-1, 1)
        if (predict_mean.ndim > 1) and (predict_mean.shape[1] != Y.shape[1]):  # multi-latent case
            pred_mean, pred_var = predict_mean[..., None], predict_var[..., None] * np.eye(predict_var.shape[1])
        else:
            pred_mean, pred_var = predict_mean.reshape(-1, 1, 1), predict_var.reshape(-1, 1, 1)
        log_density = vmap(self.likelihood.log_density, (0, 0, 0, None))(
            Y.reshape(-1, 1),
            pred_mean,
            pred_var,
            cubature
        )
        return -np.nanmean(log_density)

    def group_natural_params(self, nat1, nat2, batch_ind=None):
        if (batch_ind is not None) and (batch_ind.shape[0] != self.num_data):
            nat1 = index_update(self.pseudo_likelihood.nat1, index[batch_ind], nat1)
            nat2 = index_update(self.pseudo_likelihood.nat2, index[batch_ind], nat2)
        return nat1, nat2

    def conditional_posterior_to_data(self, batch_ind=None, post_mean=None, post_cov=None):
        if batch_ind is None:
            batch_ind = np.arange(self.num_data)
        ind = self.ind[batch_ind]
        if post_mean is None:
            post_mean = self.posterior_mean.value[ind]
        if post_cov is None:
            post_cov = self.posterior_variance.value[ind]
        return post_mean, post_cov

    def conditional_data_to_posterior(self, mean_f, cov_f):
        return mean_f, cov_f

    def expected_density_pseudo(self):
        expected_density = vmap(gaussian_expected_log_lik)(  # parallel operation
            self.pseudo_likelihood.mean,
            self.posterior_mean.value,
            self.posterior_variance.value,
            self.pseudo_likelihood.covariance,
            self.mask_pseudo_y
        )
        return np.sum(expected_density)

    def compute_full_pseudo_lik(self):
        return self.pseudo_likelihood()

    def compute_full_pseudo_nat(self, batch_ind):
        return self.pseudo_likelihood.nat1[batch_ind], self.pseudo_likelihood.nat2[batch_ind]

    def cavity_distribution(self, batch_ind=None, power=1.):
        """ Compute the power EP cavity for the given data points """
        if batch_ind is None:
            batch_ind = np.arange(self.num_data)

        nat1lik_full, nat2lik_full = self.compute_full_pseudo_nat(batch_ind)

        # then compute the cavity
        cavity_mean, cavity_cov = vmap(compute_cavity, [0, 0, 0, 0, None])(
            self.posterior_mean.value[batch_ind],
            self.posterior_variance.value[batch_ind],
            nat1lik_full,
            nat2lik_full,
            power
        )
        return cavity_mean, cavity_cov


class GP(BaseModel):
    """
    A standard (kernel-based) GP model with prior of the form
        f(t) ~ GP(0,k(t,t'))
    TODO: implement multi-latents
    """
    def __init__(self,
                 kernel,
                 likelihood,
                 X,
                 Y):
        super().__init__(kernel, likelihood, X, Y)
        self.obs_ind = np.array(np.squeeze(np.where(~self.mask_y)[0]))  # index into observed values

    def update_posterior(self):
        """
        Compute the approximate posterior distribution using standard Gaussian identities
        """
        mean, covariance = gaussian_conditional(self.kernel,
                                                self.pseudo_likelihood.mean,
                                                self.pseudo_likelihood.covariance,
                                                self.X)
        self.posterior_mean.value = mean.reshape(self.num_data, 1, 1)
        self.posterior_variance.value = np.diag(covariance).reshape(self.num_data, 1, 1)

    def compute_log_lik(self, pseudo_y=None, pseudo_var=None):
        """
        Compute the log marginal likelihood of the pseudo model, i.e. the log normaliser of the approximate posterior
        """
        dim = 1  # TODO: implement multivariate case
        # TODO: won't match MarkovGP for batching with missings or for multidim input

        X = self.X[self.obs_ind]  # only compute log lik for observed values  # TODO: check use of obs_ind (remove?)
        if pseudo_y is None:
            pseudo_y = self.pseudo_likelihood.mean
            pseudo_var = self.pseudo_likelihood.covariance
        pseudo_y = pseudo_y[self.obs_ind]
        pseudo_var = pseudo_var[self.obs_ind]

        Knn = self.kernel(X, X)
        Ky = Knn + np.diag(np.squeeze(pseudo_var))  # TODO: this will break for multi-latents

        # ---- compute the marginal likelihood, i.e. the normaliser, of the pseudo model ----
        pseudo_y = diag(pseudo_y)
        Ly, low = cho_factor(Ky)
        log_lik_pseudo = (
            - 0.5 * np.sum(pseudo_y.T @ cho_solve((Ly, low), pseudo_y))
            - np.sum(np.log(np.diag(Ly)))
            - 0.5 * pseudo_y.shape[0] * dim * LOG2PI
        )

        return log_lik_pseudo

    def predict(self, X, R=None):
        """
        predict f at new test locations X
        """
        if len(X.shape) < 2:
            X = X[:, None]
        mean, covariance = gaussian_conditional(self.kernel,
                                                self.pseudo_likelihood.mean,
                                                self.pseudo_likelihood.covariance,
                                                self.X,
                                                X)
        predict_mean = np.squeeze(mean)
        predict_variance = np.diag(covariance)
        return predict_mean, predict_variance

    def compute_kl(self):
        """
        KL divergence between the approximate posterior q(u) and the prior p(u)
        """
        # log int p(u) prod_n N(pseudo_y_n | u, pseudo_var_n) du
        log_lik_pseudo = self.compute_log_lik()
        # E_q[log N(pseudo_y_n | u, pseudo_var_n)]
        expected_density_pseudo = self.expected_density_pseudo()
        kl = expected_density_pseudo - log_lik_pseudo  # KL[approx_post || prior]
        return kl

    def prior_sample(self, num_samps=1, X=None, seed=0):
        """
        Sample from the model prior f~N(0,K) multiple times using a nested loop.
        :param num_samps: the number of samples to draw [scalar]
        :param X: the input locations at which to sample (defaults to training inputs) [N, dim]
        :param seed: the random seed for sampling
        :return:
            f_samples: the prior samples [num_samps, N, func_dim]
        """
        if X is None:
            X = self.X
        if X.ndim < 2:
            X = X[:, None]
        N = X.shape[0]
        m = np.zeros([num_samps, N, self.func_dim])
        K = self.kernel(X, X)
        chol_K = np.tile(np.linalg.cholesky(K + 1e-12 * np.eye(N)), [num_samps, 1, 1])
        gen = objax.random.Generator(seed)
        rand_norm_samples = objax.random.normal(shape=(num_samps, N, self.func_dim), generator=gen)
        f_samples = m + chol_K @ rand_norm_samples
        return f_samples


class SparseGP(GP):
    """
    A standard (kernel-based) GP model with prior of the form
        f(t) ~ GP(0,k(t,t'))
    :param opt_z: flag whether to optimise the inducing inputs Z
    TODO: write test comparing to gpflow
    TODO: implement multi-latents
    """
    def __init__(self,
                 kernel,
                 likelihood,
                 X,
                 Y,
                 Z,
                 opt_z=False):
        super().__init__(kernel, likelihood, X, Y)
        if Z.ndim < 2:
            Z = Z[:, None]
        if opt_z:
            self.Z = objax.TrainVar(Z)
        else:
            self.Z = objax.StateVar(Z)
        self.num_inducing = Z.shape[0]
        self.posterior_mean = objax.StateVar(np.zeros([self.num_inducing, self.func_dim, 1]))
        self.posterior_variance = objax.StateVar(np.tile(np.eye(self.func_dim), [self.num_inducing, 1, 1]))
        self.posterior_covariance = objax.StateVar(np.eye(self.num_inducing))

    def update_posterior(self):
        """
        Compute the approximate posterior distribution using standard Gaussian identities
        """
        mean, covariance = sparse_gaussian_conditional(self.kernel,
                                                       self.pseudo_likelihood.nat1,
                                                       self.pseudo_likelihood.nat2,
                                                       self.X,
                                                       self.Z.value)
        self.posterior_mean.value = mean.reshape(self.num_inducing, 1, 1)
        self.posterior_variance.value = np.diag(covariance).reshape(self.num_inducing, 1, 1)
        self.posterior_covariance.value = covariance.reshape(self.num_inducing, self.num_inducing)

    def compute_global_pseudo_lik(self):
        """ The pseudo-likelihoods are currently stored as N Gaussians in f """
        Kuf = self.kernel(self.Z.value, self.X[self.obs_ind])  # only compute log lik for observed values
        Kuu = self.kernel(self.Z.value, self.Z.value)
        Wuf = solve(Kuu, Kuf)  # conditional mapping, Kuu^-1 Kuf

        nat1lik_full = Wuf @ np.squeeze(self.pseudo_likelihood.nat1[self.obs_ind], axis=-1)
        nat2lik_full = Wuf @ np.diag(np.squeeze(self.pseudo_likelihood.nat2[self.obs_ind])) @ transpose(Wuf)
        pseudo_var_full = inv(nat2lik_full + 1e-12 * np.eye(Kuu.shape[0]))
        pseudo_y_full = pseudo_var_full @ nat1lik_full
        return pseudo_y_full, pseudo_var_full

    def compute_full_pseudo_lik(self):
        nat1lik_full, nat2lik_full = self.compute_full_pseudo_nat(self.obs_ind)  # TODO: remove obs_ind
        pseudo_var_full = inv_vmap(nat2lik_full + 1e-12 * np.eye(nat2lik_full.shape[1]))
        pseudo_y_full = pseudo_var_full @ nat1lik_full
        return pseudo_y_full, pseudo_var_full

    def compute_full_pseudo_nat(self, batch_ind):
        Kuf = self.kernel(self.Z.value, self.X[batch_ind].reshape(-1, 1))  # only compute log lik for observed values
        Kuu = self.kernel(self.Z.value, self.Z.value)
        Wuf = solve(Kuu, Kuf)  # conditional mapping, Kuu^-1 Kuf
        nat1lik_full = Wuf.T[..., None] @ self.pseudo_likelihood.nat1[batch_ind]
        nat2lik_full = Wuf.T[..., None] @ self.pseudo_likelihood.nat2[batch_ind] @ Wuf.T[:, None]
        return nat1lik_full, nat2lik_full

    def compute_kl(self):
        """
        KL divergence between the approximate posterior q(u) and the prior p(u)
        """
        pseudo_y_full, pseudo_var_full = self.compute_global_pseudo_lik()

        # ---- compute the log marginal likelihood, i.e. the normaliser, of the pseudo model ----
        # log int p(u) prod_n N(pseudo_y_n | u, pseudo_var_n) du
        log_lik_pseudo = self.compute_log_lik(pseudo_y_full, pseudo_var_full)

        # E_q[log N(pseudo_y_n | u, pseudo_var_n)]
        expected_density_pseudo = gaussian_expected_log_lik(  # this term does not depend on the prior, use stored q(u)
            pseudo_y_full,
            np.squeeze(self.posterior_mean.value, axis=-1),
            self.posterior_covariance.value,
            pseudo_var_full
        )

        kl = expected_density_pseudo - log_lik_pseudo  # KL[approx_post || prior]
        return kl

    def compute_log_lik(self, pseudo_y=None, pseudo_var=None):
        """ log int p(u) prod_n N(pseudo_y_n | u, pseudo_var_n) du """
        dim = 1  # TODO: implement multivariate case
        Kuu = self.kernel(self.Z.value, self.Z.value)

        Ky = Kuu + pseudo_var
        Ly, low = cho_factor(Ky)
        log_lik_pseudo = (  # this term depends on the prior
            - 0.5 * np.sum(pseudo_y.T @ cho_solve((Ly, low), pseudo_y))
            - np.sum(np.log(np.diag(Ly)))
            - 0.5 * pseudo_y.shape[0] * dim * LOG2PI
        )
        return log_lik_pseudo

    def predict(self, X, R=None):
        """
        predict at new test locations X
        """
        if len(X.shape) < 2:
            X = X[:, None]
        self.update_posterior()
        mean, covariance = sparse_conditional_post_to_data(self.kernel,
                                                           self.posterior_mean.value,
                                                           self.posterior_covariance.value,
                                                           X,
                                                           self.Z.value)
        predict_mean = np.squeeze(mean)
        predict_variance = np.diag(covariance)
        return predict_mean, predict_variance

    def conditional_posterior_to_data(self, batch_ind=None, post_mean=None, post_cov=None):
        """
        compute
        q(f) = int p(f | u) q(u) du
        where
        q(u) = N(u | post_mean, post_cov)
        """
        if batch_ind is None:
            batch_ind = np.arange(self.num_data)
        if post_mean is None:
            post_mean = self.posterior_mean.value
        if post_cov is None:
            post_cov = self.posterior_covariance.value

        ndim = post_cov.ndim
        Nbatch = batch_ind.shape[0]

        if ndim < 3:
            mean_f, cov_f = sparse_conditional_post_to_data(self.kernel,
                                                            post_mean,
                                                            post_cov,
                                                            self.X[batch_ind],
                                                            self.Z.value)
            return mean_f.reshape(Nbatch, 1, 1), np.diag(cov_f).reshape(Nbatch, 1, 1)
        else:  # in the PEP case we have one cavity per data point
            mean_f, cov_f = vmap(sparse_conditional_post_to_data, [None, 0, 0, 0, None])(self.kernel,
                                                                                         post_mean,
                                                                                         post_cov,
                                                                                         self.X[batch_ind],
                                                                                         self.Z.value)
            return mean_f.reshape(Nbatch, 1, 1), cov_f.reshape(Nbatch, 1, 1)

    def cavity_distribution(self, batch_ind=None, power=1.):
        """ Compute the power EP cavity for the given data points """
        if batch_ind is None:
            batch_ind = np.arange(self.num_data)

        nat1lik_full, nat2lik_full = self.compute_full_pseudo_nat(batch_ind)

        # then compute the cavity
        cavity_mean, cavity_cov = vmap(compute_cavity, [None, None, 0, 0, None])(
            self.posterior_mean.value[..., 0],
            self.posterior_covariance.value,
            nat1lik_full,
            nat2lik_full,
            power
        )
        return cavity_mean, cavity_cov


class MarkovGP(BaseModel):
    """
    The stochastic differential equation (SDE) form of a Gaussian process (GP) model.
    Implements methods for inference and learning using state space methods, i.e. Kalman filtering and smoothing.
    Constructs a linear time-invariant (LTI) stochastic differential equation (SDE) of the following form:
        dx(t)/dt = F x(t) + L w(t)
              yₙ ~ p(yₙ | f(t_n)=H x(t_n))
    where w(t) is a white noise process and where the state x(t) is Gaussian distributed with initial
    state distribution x(t)~𝓝(0,Pinf).
    """
    def __init__(self,
                 kernel,
                 likelihood,
                 X,
                 Y,
                 R=None,
                 parallel=None):
        if parallel is None:  # if using a GPU, then run the parallel filter
            parallel = xla_bridge.get_backend().platform == 'gpu'
        (X, Y, self.R, self.dt) = input_admin(X, Y, R)
        H = kernel.measurement_model()
        func_dim = H.shape[0]  # number of latent dimensions
        super().__init__(kernel, likelihood, X, Y, func_dim=func_dim)
        self.state_dim = self.kernel.stationary_covariance().shape[0]
        self.minf = np.zeros([self.state_dim, 1])  # stationary state mean
        self.spatio_temporal = np.any(~np.isnan(self.R))
        self.parallel = parallel
        if (self.func_dim != self.obs_dim) and self.spatio_temporal:
            self.mask_pseudo_y = None  # sparse spatio-temporal case, no mask required

    @staticmethod
    def filter(*args, **kwargs):
        return kalman_filter(*args, **kwargs)

    @staticmethod
    def smoother(*args, **kwargs):
        return rauch_tung_striebel_smoother(*args, **kwargs)

    @staticmethod
    def temporal_conditional(*args, **kwargs):
        return temporal_conditional(*args, **kwargs)

    def compute_full_pseudo_nat(self, batch_ind):
        if self.spatio_temporal:  # spatio-temporal case
            B, C = self.kernel.spatial_conditional(self.X[batch_ind], self.R[batch_ind])
            nat1lik_full = transpose(B) @ self.pseudo_likelihood.nat1[batch_ind]
            nat2lik_full = transpose(B) @ self.pseudo_likelihood.nat2[batch_ind] @ B
            return nat1lik_full, nat2lik_full
        else:  # temporal case
            return self.pseudo_likelihood.nat1[batch_ind], self.pseudo_likelihood.nat2[batch_ind]

    def compute_full_pseudo_lik(self):
        # TODO: running this 3 times per training loop is wasteful - store in memory?
        if self.spatio_temporal:  # spatio-temporal case
            B, C = self.kernel.spatial_conditional(self.X, self.R)
            # TODO: more efficient way to do this?
            nat1lik_full = transpose(B) @ self.pseudo_likelihood.nat1
            nat2lik_full = transpose(B) @ self.pseudo_likelihood.nat2 @ B
            pseudo_var_full = inv_vmap(nat2lik_full + 1e-12 * np.eye(nat2lik_full.shape[1]))  # <---------- bottleneck
            pseudo_y_full = pseudo_var_full @ nat1lik_full
            return pseudo_y_full, pseudo_var_full
        else:  # temporal case
            return self.pseudo_likelihood.mean, self.pseudo_likelihood.covariance

    def update_posterior(self):
        """
        Compute the posterior via filtering and smoothing
        """
        pseudo_y, pseudo_var = self.compute_full_pseudo_lik()
        log_lik, (filter_mean, filter_cov) = self.filter(self.dt,
                                                         self.kernel,
                                                         pseudo_y,
                                                         pseudo_var,
                                                         mask=self.mask_pseudo_y,
                                                         parallel=self.parallel)
        dt = np.concatenate([self.dt[1:], np.array([0.0])], axis=0)
        smoother_mean, smoother_cov, _ = self.smoother(dt,
                                                       self.kernel,
                                                       filter_mean,
                                                       filter_cov,
                                                       parallel=self.parallel)
        self.posterior_mean.value, self.posterior_variance.value = smoother_mean, smoother_cov

    def compute_kl(self):
        """
        KL[q()|p()]
        """
        pseudo_y, pseudo_var = self.compute_full_pseudo_lik()
        log_lik_pseudo = self.compute_log_lik(pseudo_y, pseudo_var)

        expected_density_pseudo = vmap(gaussian_expected_log_lik)(  # parallel operation
            pseudo_y,
            self.posterior_mean.value,
            self.posterior_variance.value,
            pseudo_var,
            self.mask_pseudo_y
        )

        kl = np.sum(expected_density_pseudo) - log_lik_pseudo  # KL[approx_post || prior]
        return kl

    def compute_log_lik(self, pseudo_y=None, pseudo_var=None):
        """
        int p(f) N(pseudo_y | f, pseudo_var) df
        """
        if pseudo_y is None:
            pseudo_y, pseudo_var = self.compute_full_pseudo_lik()

        log_lik_pseudo, (_, _) = self.filter(
            self.dt,
            self.kernel,
            pseudo_y,
            pseudo_var,
            mask=self.mask_pseudo_y,
            parallel=self.parallel
        )
        return log_lik_pseudo

    def conditional_posterior_to_data(self, batch_ind=None, post_mean=None, post_cov=None):
        """
        compute
        q(f) = int p(f | u) q(u) du = N(f | B post_mean, B post_cov B' + C)
        where
        q(u) = N(u | post_mean, post_cov)
        p(f | u) = N(f | Bu, C)
        """
        if batch_ind is None:
            batch_ind = np.arange(self.num_data)
        if post_mean is None:
            post_mean = self.posterior_mean.value[batch_ind]
        if post_cov is None:
            post_cov = self.posterior_variance.value[batch_ind]

        if self.spatio_temporal:
            B, C = self.kernel.spatial_conditional(self.X[batch_ind], self.R[batch_ind])
            mean_f = B @ post_mean
            cov_f = B @ post_cov @ transpose(B) + C
            return mean_f, cov_f
        else:
            return post_mean, post_cov

    def predict(self, X=None, R=None, pseudo_lik_params=None):
        """
        predict at new test locations X
        """
        if X is None:
            X = self.X
        elif len(X.shape) < 2:
            X = X[:, None]
        if R is None:
            R = X[:, 1:]
        X = X[:, :1]  # take only the temporal component

        if pseudo_lik_params is None:
            pseudo_y, pseudo_var = self.compute_full_pseudo_lik()
        else:
            pseudo_y, pseudo_var = pseudo_lik_params  # this deals with the posterior sampling case
        _, (filter_mean, filter_cov) = self.filter(self.dt,
                                                   self.kernel,
                                                   pseudo_y,
                                                   pseudo_var,
                                                   mask=self.mask_pseudo_y,  # mask has no effect here (loglik not used)
                                                   parallel=self.parallel)
        dt = np.concatenate([self.dt[1:], np.array([0.0])], axis=0)
        smoother_mean, smoother_cov, gain = self.smoother(dt,
                                                          self.kernel,
                                                          filter_mean,
                                                          filter_cov,
                                                          return_full=True,
                                                          parallel=self.parallel)

        # add dummy states at either edge
        inf = 1e10 * np.ones_like(self.X[0, :1])
        X_aug = np.block([[-inf], [self.X[:, :1]], [inf]])

        # predict the state distribution at the test time steps:
        state_mean, state_cov = self.temporal_conditional(X_aug, X, smoother_mean, smoother_cov, gain, self.kernel)
        # extract function values from the state:
        H = self.kernel.measurement_model()
        if self.spatio_temporal:
            # TODO: if R is fixed, only compute B, C once
            B, C = self.kernel.spatial_conditional(X, R)
            W = B @ H
            test_mean = W @ state_mean
            test_var = W @ state_cov @ transpose(W) + C
        else:
            test_mean, test_var = H @ state_mean, H @ state_cov @ transpose(H)

        if np.squeeze(test_var).ndim > 2:  # deal with spatio-temporal case (discard spatial covariance)
            test_var = diag(np.squeeze(test_var))
        return np.squeeze(test_mean), np.squeeze(test_var)

    def filter_energy(self):
        pseudo_y, pseudo_var = self.compute_full_pseudo_lik()
        _, (filter_mean, filter_cov) = self.filter(self.dt,
                                                   self.kernel,
                                                   pseudo_y,
                                                   pseudo_var,
                                                   mask=self.mask_pseudo_y,  # mask has no effect here (loglik not used)
                                                   parallel=self.parallel,
                                                   return_predict=True)
        H = self.kernel.measurement_model()
        mean = H @ filter_mean
        var = H @ filter_cov @ transpose(H)
        filter_energy = -np.sum(vmap(self.likelihood.log_density)(self.Y, mean, var))
        return filter_energy

    def prior_sample(self, num_samps=1, X=None, seed=0):
        """
        Sample from the model prior f~N(0,K) multiple times using a nested loop.
        :param num_samps: the number of samples to draw [scalar]
        :param X: the input locations at which to sample (defaults to training inputs) [N, 1]
        :param seed: the random seed for sampling
        :return:
            f_samples: the prior samples [num_samps, N, func_dim]
        """
        if X is None:
            dt = self.dt
        else:
            dt = np.concatenate([np.array([0.0]), np.diff(np.sort(X))])
        sd = self.state_dim
        H = self.kernel.measurement_model()
        Pinf = self.kernel.stationary_covariance()
        As = vmap(self.kernel.state_transition)(dt)
        Qs = vmap(process_noise_covariance, [0, None])(As, Pinf)
        jitter = 1e-8 * np.eye(sd)
        f0 = np.zeros([dt.shape[0], self.func_dim, 1])

        def draw_full_sample(carry_, _):
            f_sample_i, i = carry_
            gen0 = objax.random.Generator(seed - 1 - i)
            m0 = np.linalg.cholesky(Pinf) @ objax.random.normal(shape=(sd, 1), generator=gen0)

            def sample_one_time_step(carry, inputs):
                m, k = carry
                A, Q = inputs
                chol_Q = np.linalg.cholesky(Q + jitter)  # <--- can be a bit unstable
                gen = objax.random.Generator(seed + i * k + k)
                q_samp = chol_Q @ objax.random.normal(shape=(sd, 1), generator=gen)
                m = A @ m + q_samp
                f = H @ m
                return (m, k+1), f

            (_, _), f_sample = scan(f=sample_one_time_step,
                                    init=(m0, 0),
                                    xs=(As, Qs))

            return (f_sample, i+1), f_sample

        (_, _), f_samples = scan(f=draw_full_sample,
                                 init=(f0, 0),
                                 xs=np.zeros(num_samps))

        return f_samples

    def posterior_sample(self, X=None, num_samps=1, seed=0):
        """
        Sample from the posterior at the test locations.
        Posterior sampling works by smoothing samples from the prior using the approximate Gaussian likelihood
        model given by the pseudo-likelihood, 𝓝(f|μ*,σ²*), computed during training.
         - draw samples (f*) from the prior
         - add Gaussian noise to the prior samples using auxillary model p(y*|f*) = 𝓝(y*|f*,σ²*)
         - smooth the samples by computing the posterior p(f*|y*)
         - posterior samples = prior samples + smoothed samples + posterior mean
                             = f* + E[p(f*|y*)] + E[p(f|y)]
        See Arnaud Doucet's note "A Note on Efficient Conditional Simulation of Gaussian Distributions" for details.
        :param X: the sampling input locations [N, 1]
        :param num_samps: the number of samples to draw [scalar]
        :param seed: the random seed for sampling
        :return:
            the posterior samples [N_test, num_samps]
        """
        if X is None:
            train_ind = np.arange(self.num_data)
            test_ind = train_ind
        else:
            if X.ndim < 2:
                X = X[:, None]
            X = np.concatenate([self.X, X])
            X, ind = np.unique(X, return_inverse=True)
            train_ind, test_ind = ind[:self.num_data], ind[self.num_data:]
        post_mean, _ = self.predict(X)
        prior_samp = self.prior_sample(X=X, num_samps=num_samps, seed=seed)  # sample at training locations
        lik_chol = np.tile(np.linalg.cholesky(self.pseudo_likelihood.covariance), [num_samps, 1, 1, 1])
        gen = objax.random.Generator(seed)
        prior_samp_train = prior_samp[:, train_ind]
        prior_samp_y = prior_samp_train + lik_chol @ objax.random.normal(shape=prior_samp_train.shape, generator=gen)

        def smooth_prior_sample(i, prior_samp_y_i):
            smoothed_sample, _ = self.predict(X, pseudo_lik_params=(prior_samp_y_i, self.pseudo_likelihood.covariance))
            return i+1, smoothed_sample

        _, smoothed_samples = scan(f=smooth_prior_sample,
                                   init=0,
                                   xs=prior_samp_y)

        return (prior_samp[..., 0, 0] - smoothed_samples + post_mean[None])[:, test_ind]


class SparseMarkovGP(MarkovGP):
    """
    A sparse Markovian GP.
    TODO: implement version with non-tied sites
    """
    def __init__(self,
                 kernel,
                 likelihood,
                 X,
                 Y,
                 Z,
                 R=None,
                 parallel=None):
        super().__init__(kernel, likelihood, X, Y, R=R, parallel=parallel)
        if Z.ndim < 2:
            Z = Z[:, None]
        Z = np.sort(Z, axis=0)
        inf = np.array([[1e10]])
        self.Z = objax.StateVar(np.concatenate([-inf, Z, inf], axis=0))
        self.dz = np.array(np.diff(self.Z.value[:, 0]))
        self.num_transitions = self.dz.shape[0]
        zeros = np.zeros([self.num_transitions, 2 * self.state_dim, 1])
        eyes = np.tile(np.eye(2 * self.state_dim), [self.num_transitions, 1, 1])

        # nat2 = 1e-8 * eyes

        # initialise to match MarkovGP / GP on first step (when Z=X):
        nat2 = index_update(1e-8 * eyes, index[:-1, self.state_dim, self.state_dim], 1e-2)

        # initialise to match old implementation:
        # nat2 = (1 / 99) * eyes

        self.pseudo_likelihood = GaussianDistribution(
            zeros,
            inv_vmap(nat2)
        )
        self.posterior_mean = objax.StateVar(zeros)
        self.posterior_variance = objax.StateVar(eyes)
        self.mask_pseudo_y = None
        self.conditional_mean = None
        # TODO: if training Z this needs to be done at every training step (as well as sorting and computing dz)
        self.ind, self.num_neighbours = set_z_stats(self.X, self.Z.value)

    @staticmethod
    def filter(*args, **kwargs):
        return kalman_filter_pairs(*args, **kwargs)

    @staticmethod
    def smoother(*args, **kwargs):
        return rauch_tung_striebel_smoother(*args, **kwargs)

    def compute_full_pseudo_lik(self):
        return self.pseudo_likelihood()

    def update_posterior(self):
        """
        Compute the posterior via filtering and smoothing
        """
        log_lik, (filter_mean, filter_cov) = self.filter(self.dz,
                                                         self.kernel,
                                                         self.pseudo_likelihood.mean,
                                                         self.pseudo_likelihood.covariance,
                                                         parallel=self.parallel)
        dz = self.dz[1:]
        smoother_mean, smoother_cov, gain = self.smoother(dz,
                                                          self.kernel,
                                                          filter_mean,
                                                          filter_cov,
                                                          return_full=True,
                                                          parallel=self.parallel)

        minf, Pinf = self.minf[None, ...], self.kernel.stationary_covariance()[None, ...]
        mean_aug = np.concatenate([minf, smoother_mean, minf])
        cov_aug = np.concatenate([Pinf, smoother_cov, Pinf])
        gain = np.concatenate([np.zeros_like(gain[:1]), gain])
        # construct the joint distribution between neighbouring pairs of states
        post_mean, post_cov = vmap(build_joint, [0, None, None, None])(
            np.arange(self.num_transitions), mean_aug, cov_aug, gain
        )

        self.posterior_mean.value, self.posterior_variance.value = post_mean, post_cov

    def compute_log_lik(self, pseudo_y=None, pseudo_var=None):
        """
        Compute the log marginal likelihood of the pseudo model, i.e. the log normaliser of the approximate posterior
        """
        log_lik, (_, _) = self.filter(self.dz,
                                      self.kernel,
                                      self.pseudo_likelihood.mean,
                                      self.pseudo_likelihood.covariance,
                                      parallel=self.parallel)
        return log_lik

    def compute_kl(self):
        """
        KL divergence between the approximate posterior q(u) and the prior p(u)
        TODO: can we remove the need for this by generalising these methods?
        """
        # log int p(u) prod_n N(pseudo_y_n | u, pseudo_var_n) du
        log_lik_pseudo = self.compute_log_lik()
        # E_q[log N(pseudo_y_n | u, pseudo_var_n)]
        expected_density_pseudo = self.expected_density_pseudo()
        kl = expected_density_pseudo - log_lik_pseudo  # KL[approx_post || prior]
        return kl

    def predict(self, X, R=None):
        """
        predict at new test locations X
        """
        if len(X.shape) < 2:
            X = X[:, None]
        if R is None:
            R = X[:, 1:]
        X = X[:, :1]  # take only the temporal component

        _, (filter_mean, filter_cov) = self.filter(self.dz,
                                                   self.kernel,
                                                   self.pseudo_likelihood.mean,
                                                   self.pseudo_likelihood.covariance,
                                                   parallel=self.parallel)
        dz = self.dz[1:]
        smoother_mean, smoother_cov, gain = self.smoother(dz,
                                                          self.kernel,
                                                          filter_mean,
                                                          filter_cov,
                                                          return_full=True,
                                                          parallel=self.parallel)

        # predict the state distribution at the test time steps
        state_mean, state_cov = self.temporal_conditional(self.Z.value, X, smoother_mean, smoother_cov,
                                                          gain, self.kernel)
        # extract function values from the state:
        H = self.kernel.measurement_model()
        if self.spatio_temporal:
            # TODO: if R is fixed, only compute B, C once
            B, C = self.kernel.spatial_conditional(X, R)
            W = B @ H
            test_mean = W @ state_mean
            test_var = W @ state_cov @ transpose(W) + C
        else:
            test_mean, test_var = H @ state_mean, H @ state_cov @ transpose(H)

        if np.squeeze(test_var).ndim > 2:  # deal with spatio-temporal case (discard spatial covariance)
            test_var = diag(np.squeeze(test_var))
        return np.squeeze(test_mean), np.squeeze(test_var)

    def conditional_posterior_to_data(self, batch_ind=None, post_mean=None, post_cov=None):
        """
        compute
        q(f) = int p(f | u) q(u) du
        where
        q(u) = N(u | post_mean, post_cov)
        """
        if batch_ind is None:
            batch_ind = np.arange(self.num_data)
        if post_mean is None:
            post_mean = self.posterior_mean.value
        if post_cov is None:
            post_cov = self.posterior_variance.value
        ind = self.ind[batch_ind]
        post_mean, post_cov = post_mean[ind], post_cov[ind]

        P, T = vmap(compute_conditional_statistics, [0, None, None, 0])(
            self.X[batch_ind, :1], self.Z.value, self.kernel, ind
        )

        H = self.kernel.measurement_model()
        if self.spatio_temporal:
            B, C = self.kernel.spatial_conditional(self.X[batch_ind], self.R[batch_ind])
            BH = B @ H
            self.conditional_mean = BH @ P  # W
            conditional_cov = BH @ T @ transpose(BH) + C  # nu
        else:
            self.conditional_mean = H @ P  # W
            conditional_cov = H @ T @ transpose(H)  # nu

        mean_f = self.conditional_mean @ post_mean
        cov_f = self.conditional_mean @ post_cov @ transpose(self.conditional_mean) + conditional_cov

        return mean_f, cov_f

    def conditional_data_to_posterior(self, mean_f, cov_f):
        """
        conditional_posterior_to_data() must be run first so that self.conditional_mean is set
        """
        mean_q = transpose(self.conditional_mean) @ mean_f
        cov_q = transpose(self.conditional_mean) @ cov_f @ self.conditional_mean
        return mean_q, cov_q

    def group_natural_params(self, nat1_n, nat2_n, batch_ind=None):

        if batch_ind is None:
            ind = self.ind
        else:
            ind = self.ind[batch_ind]

        old_nat1 = self.pseudo_likelihood.nat1
        old_nat2 = self.pseudo_likelihood.nat2

        (new_nat1, new_nat2, counter), _ = scan(f=sum_natural_params_by_group,
                                                init=(np.zeros_like(old_nat1),
                                                      np.zeros_like(old_nat2),
                                                      np.zeros(old_nat1.shape[0])),
                                                xs=(ind, nat1_n, nat2_n))

        num_neighbours = np.maximum(self.num_neighbours, 1).reshape(-1, 1, 1)
        counter = counter.reshape(-1, 1, 1)
        nat1 = new_nat1 + (1. - counter / num_neighbours) * old_nat1
        nat2 = new_nat2 + (1. - counter / num_neighbours) * old_nat2

        nat2 += 1e-8 * np.eye(nat2.shape[1])  # prevent zeros

        return nat1, nat2

    def cavity_distribution(self, batch_ind=None, power=None):
        """ Compute the power EP cavity for the given data points """
        fraction = power / np.maximum(self.num_neighbours, 1)
        cavity_mean, cavity_cov = vmap(compute_cavity)(
            self.posterior_mean.value,
            self.posterior_variance.value,
            self.pseudo_likelihood.nat1,
            self.pseudo_likelihood.nat2,
            fraction
        )
        return cavity_mean, cavity_cov
