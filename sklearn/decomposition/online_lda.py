"""

=============================================================
Online Latent Dirichlet Allocation with variational inference
=============================================================

This implementation is modified from Matthew D. Hoffman's onlineldavb code
Link: http://www.cs.princeton.edu/~mdhoffma/code/onlineldavb.tar
"""

# Author: Chyi-Kwei Yau
# Author: Matthew D. Hoffman (original onlineldavb implementation)

import numpy as np
import scipy.sparse as sp
from scipy.special import gammaln

from ..base import BaseEstimator, TransformerMixin
from ..utils import (check_random_state, check_array,
                     gen_batches, gen_even_slices, _get_n_jobs)
from ..utils.validation import NotFittedError, check_non_negative

from ..externals.joblib import Parallel, delayed
from ..externals.six.moves import xrange

from ._online_lda import (mean_change, _dirichlet_expectation_1d,
                          _dirichlet_expectation_2d)

EPS = np.finfo(np.float).eps


def _log_dirichlet_expectation(X):
    """Calculate log Dirichlet expectation.

    For an array theta ~ Dir(X), computes `E[log(theta)]` given X.

    Parameters
    ----------
    X : array-like
        1 or 2 dimensional vector

    Returns
    -------
    dirichlet_expection : array-like
        Dirichlet expectation of input array X
    """

    if len(X.shape) == 1:
        dirichlet_expection = _dirichlet_expectation_1d(X)
    else:
        dirichlet_expection = _dirichlet_expectation_2d(X)
    return dirichlet_expection


def _update_doc_distribution(X, exp_topic_word_distr, doc_topic_prior, max_iters,
                             mean_change_tol, cal_sstats, rng):
    """E-step: update document-topic distribution.

    Parameters
    ----------
    X : array-like or sparse matrix, shape=(n_samples, n_features)
        Document word matrix.

    exp_topic_word_distr : dense matrix, shape=(n_topics, n_features)
        Exponential value of expection of log topic word distribution.
        In the literature, this is `exp(E[log(beta)])`.

    doc_topic_prior : float
        Prior of document topic distribution `theta`.

    max_iters : int
        Max number of iterations for updating document topic distribution in E-step.

    mean_change_tol : float
        Stopping tolerance for updating document topic distribution in E-setp.

    cal_sstats : boolean
        Parameter that indicate to calculate sufficient statistics or not.
        Set `cal_sstats` to `True` when we need to run M-step.

    rng : RandomState instance or None
        Parameter that indicate how to initialize document topic distribution.
        Set `rng` to None will initialize document topic distribution to a
        constant number.

    Returns
    -------
    (doc_topic_distr, suff_stats) :
        `doc_topic_distr` is unnormalized topic distribution for each document.
        In the literature, this is `gamma`. we can calcuate `E[log(theta)]`
        from it.
        `suff_stats` is expected sufficient statistics for the M-step.
            When `cal_sstats == False`, this will be None.

    """
    is_sparse_x = sp.issparse(X)
    n_samples, n_features = X.shape
    n_topics = exp_topic_word_distr.shape[0]

    if rng:
        doc_topic_distr = rng.gamma(100., 0.01, (n_samples, n_topics))
    else:
        doc_topic_distr = np.ones((n_samples, n_topics))

    # In the literature, this is `exp(E[log(theta)])`
    exp_doc_topic = np.exp(_log_dirichlet_expectation(doc_topic_distr))

    # diff on `component_` (only calculate it when `cal_diff` is True)
    suff_stats = np.zeros(exp_topic_word_distr.shape) if cal_sstats else None

    if is_sparse_x:
        X_data = X.data
        X_indices = X.indices
        X_indptr = X.indptr

    for idx_d in xrange(n_samples):
        if is_sparse_x:
            ids = X_indices[X_indptr[idx_d]:X_indptr[idx_d + 1]]
            cnts = X_data[X_indptr[idx_d]:X_indptr[idx_d + 1]]
        else:
            ids = np.nonzero(X[idx_d, :])[0]
            cnts = X[idx_d, ids]

        doc_topic_d = doc_topic_distr[idx_d, :]
        exp_doc_topic_d = exp_doc_topic[idx_d, :]
        exp_topic_word_d = exp_topic_word_distr[:, ids]

        # The optimal phi_{dwk} is proportional to
        # exp(E[log(theta_{dk})]) * exp(E[log(beta_{dw})]).
        norm_phi = np.dot(exp_doc_topic_d, exp_topic_word_d) + EPS

        # Iterate between `doc_topic_d` and `norm_phi` until convergence
        for _ in xrange(0, max_iters):
            last_d = doc_topic_d

            doc_topic_d = (doc_topic_prior + exp_doc_topic_d *
                           np.dot(cnts / norm_phi, exp_topic_word_d.T))
            exp_doc_topic_d = np.exp(_log_dirichlet_expectation(doc_topic_d))
            norm_phi = np.dot(exp_doc_topic_d, exp_topic_word_d) + EPS

            meanchange = mean_change(last_d, doc_topic_d)
            if meanchange < mean_change_tol:
                break
        doc_topic_distr[idx_d, :] = doc_topic_d

        # Contribution of document d to the expected sufficient
        # statistics for the M step.
        if cal_sstats:
            suff_stats[:, ids] += np.outer(exp_doc_topic_d, cnts / norm_phi)

    return (doc_topic_distr, suff_stats)


class LatentDirichletAllocation(BaseEstimator, TransformerMixin):
    """Latent Dirichlet Allocation with online variational Bayes algorithm

    Parameters
    ----------
    n_topics : int, optional (default=10)
        Number of topics.

    doc_topic_prior : float, optional (default=None)
        Prior of document topic distribution `theta`. If the value is None, defaults
        to `1 / n_topics`.
        In the literature, this is called `alpha`.

    topic_word_prior : float, optional (default=None)
        Prior of topic word distribution `beta`. If the value is None, defaults
        to `1 / n_topics`.
        In the literature, this is called `eta`.

    learning_method : 'batch' | 'online', default='online'
        Method used to update `_component`. Only used in `fit` method.
        In general, if the data size is large, the online update will be much faster than
        the batch update.
        Valid options::

            'batch': Batch variational Bayes method. Use all training data in each EM update.
                Old `components_` will be overwritten in each iteration.
            'online': Online variational Bayes method. In each EM update, use mini-batch of
                training data to update the `components_` variable incrementally. The learning
                rate is controlled by the `learning_decay` and the `learning_offset` parameter.

    learning_decay : float, optional (default=0.7)
        It is a parameter that control learning rate in the online learning method.
        The value should be set between (0.5, 1.0] to guarantee asymptotic convergence.
        When the value is 0.0 and batch_size is `n_samples`, the update method is same as
        batch learning. In the literature, this is called `kappa`.

    learning_offset : float, optional (default=10.)
        A (positive) parameter that downweights early iterations in online learning.
        It should be greater than 1.0. In the literature, this is called `tau0`.

    max_iter : integer, optional (default=10)
        The maximum number of iterations.

    total_samples : int, optional (default=1e6)
        Total number of documents. It is only used in the `partial_fit` method.

    batch_size : int, optional (default=128)
        Number of documents to use in each EM iteration. Only used in online learning.

    evaluate_every : int optional (default=0)
        How often to evaluate perplexity. Only used in `fit` method.
        set it to 0 or and negative number to not evalute perplexity in training at all.
        Evaluating perplexity can help you check convergence in training process, but it
        will also increase total training time.
        Evaluating perplexity in every iteration might increase training time up to 2X.

    perp_tol : float, optional (default=1e-1)
        Perplexity tolerance in batch learning. Only used when `evaluate_every` is
        greater than 0.

    mean_change_tol : float, optional (default=1e-3)
        Stopping tolerance for updating document topic distribution in E-setp.

    max_doc_update_iter : int (default=100)
        Max number of iterations for updating document topic distribution in E-step.

    n_jobs : int, optional (default=1)
        The number of jobs to use in E-step. If -1 all CPUs are used. For n_jobs
        below -1, (n_cpus + 1 + n_jobs) are used.

    verbose : int, optional (default=0)
        Verbosity level.

    random_state : int or RandomState instance or None, optional (default=None)
        Pseudo Random Number generator seed control.


    Attributes
    ----------
    components_ : array, [n_topics, n_features]
        Topic word distribution. components_[i, j] represents word `j` in topic `i`.
        In the literature, this is called `lambda`.

    n_iter_ : int
        Number of iteration.


    References
    ----------
    [1] "Online Learning for Latent Dirichlet Allocation", Matthew D. Hoffman,
        David M. Blei, Francis Bach, 2010

    [2] "Stochastic Variational Inference", Matthew D. Hoffman, David M. Blei,
        Chong Wang, John Paisley, 2013

    [3] Matthew D. Hoffman's onlineldavb code. Link:
        http://www.cs.princeton.edu/~mdhoffma/code/onlineldavb.tar

    """

    def __init__(self, n_topics=10, doc_topic_prior=None, topic_word_prior=None,
                 learning_method='online', learning_decay=.7, learning_offset=10.,
                 max_iter=10, batch_size=128, evaluate_every=-1, total_samples=1e6,
                 perp_tol=1e-1, mean_change_tol=1e-3, max_doc_update_iter=100,
                 n_jobs=1, verbose=0, random_state=None):
        self.n_topics = n_topics
        self.doc_topic_prior = doc_topic_prior
        self.topic_word_prior = topic_word_prior
        self.learning_method = learning_method
        self.learning_decay = learning_decay
        self.learning_offset = learning_offset
        self.max_iter = max_iter
        self.batch_size = batch_size
        self.evaluate_every = evaluate_every
        self.total_samples = total_samples
        self.perp_tol = perp_tol
        self.mean_change_tol = mean_change_tol
        self.max_doc_update_iter = max_doc_update_iter
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.random_state = random_state

    def _check_params(self):
        """Check model parameters."""

        if self.n_topics <= 0:
            err_msg = "Invalid 'n_topics' parameter: %r" % self.n_topics
            raise ValueError(err_msg)

        if self.total_samples <= 0:
            err_msg = "Invalid 'total_samples' parameter: %r" % self.total_samples
            raise ValueError(err_msg)

        if self.learning_offset < 0:
            err_msg = "Invalid 'learning_offset' parameter: %r" % self.learning_offset
            raise ValueError(err_msg)

        if self.learning_method not in ("batch", "online"):
            err_msg = "Invalid 'learning_method' parameter: %r" % self.learning_method
            raise ValueError(err_msg)

    def _init_latent_vars(self, n_features):
        """Initialize latent variables."""

        self.random_state_ = check_random_state(self.random_state)
        self.n_iter_ = 1

        if self.doc_topic_prior is None:
            self.doc_topic_prior_ = 1. / self.n_topics
        else:
            self.doc_topic_prior_ = self.doc_topic_prior

        if self.topic_word_prior is None:
            self.topic_word_prior_ = 1. / self.n_topics
        else:
            self.topic_word_prior_ = self.topic_word_prior

        init_gamma = 100.
        init_var = 1. / init_gamma
        # In the literature, this is called `lambda`
        self.components_ = self.random_state_.gamma(
            init_gamma, init_var, (self.n_topics, n_features))
        # In the literature, this is `E[log(beta)]`
        self.dirichlet_component_ = _log_dirichlet_expectation(self.components_)
        # In the literature, this is `exp(E[log(beta)])`
        self.exp_dirichlet_component_ = np.exp(self.dirichlet_component_)

    def _e_step(self, X, cal_sstats, random_init):
        """E-step in EM update.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        cal_sstats : boolean
            Parameter that indicate whether to calculate sufficient statistics or not.
            Set `cal_sstats` to `True` when we need to run M-step.

        random_init : boolean
            Parameter that indicate whether to initialize document topic distribution
            randomly in the E-step. Set it to `True` in training steps.

        Returns
        -------
        (doc_topic_distr, suff_stats) :
            `doc_topic_distr` is unnormailzed topic distribution for each document.
            In the literature, this is called `gamma`.
            `suff_stats` is expected sufficient statistics for the M-step.
            When `cal_sstats == False`, it will be None.

        """

        # Run e-step in parallel
        n_jobs = _get_n_jobs(self.n_jobs)
        rng = self.random_state_ if random_init else None

        results = Parallel(n_jobs=n_jobs, verbose=self.verbose)(
            delayed(_update_doc_distribution)
            (X[idx_slice, :], self.exp_dirichlet_component_, self.doc_topic_prior_,
             self.max_doc_update_iter, self.mean_change_tol, cal_sstats, rng)
            for idx_slice in gen_even_slices(X.shape[0], n_jobs))

        # merge result
        doc_topics, sstats_list = zip(*results)
        doc_topic_distr = np.vstack(doc_topics)

        if cal_sstats:
            # This step finishes computing the sufficient statistics for the M step
            suff_stats = np.zeros(self.components_.shape)
            for sstats in sstats_list:
                suff_stats += sstats
            suff_stats *= self.exp_dirichlet_component_
        else:
            suff_stats = None

        return (doc_topic_distr, suff_stats)

    def _em_step(self, X, total_samples, batch_update):
        """EM update for 1 iteration.

        update `_component` by batch VB or online VB.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        total_samples : integer
            Total umber of documents. It is only used when
            batch_update is `False`.

        batch_update : boolean
            Parameter that controls updating method.
            `True` for batch learning, `False` for online learning.

        Returns
        -------
        doc_topic_distr : array, shape=(n_samples, n_topics)
            Unnormalized document topic distribution.
        """

        # E-step
        _, suff_stats = self._e_step(X, cal_sstats=True, random_init=True)

        # M-step
        if batch_update:
            self.components_ = self.topic_word_prior_ + suff_stats
        else:
            # online update
            # In the literature, the weight is `rho`
            weight = np.power(self.learning_offset + self.n_iter_, -self.learning_decay)
            doc_ratio = float(total_samples) / X.shape[0]
            self.components_ *= (1 - weight)
            self.components_ += (weight *
                                 (self.topic_word_prior_ + doc_ratio * suff_stats))

        # update `component_` related variables
        self.dirichlet_component_ = _log_dirichlet_expectation(self.components_)
        self.exp_dirichlet_component_ = np.exp(self.dirichlet_component_)
        self.n_iter_ += 1
        return

    def _check_non_neg_array(self, X, whom):
        """check X format

        check X format and make sure no negative value in X.

        Parameters
        ----------
        X :  array-like or sparse matrix

        """
        X = check_array(X, accept_sparse='csr')
        check_non_negative(X, whom)
        return X

    def partial_fit(self, X, y=None):
        """Online VB with Mini-Batch update.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        Returns
        -------
        self
        """
        self._check_params()
        X = self._check_non_neg_array(X, "LatentDirichletAllocation.partial_fit")
        n_samples, n_features = X.shape
        batch_size = self.batch_size

        # initialize parameters or check
        if not hasattr(self, 'components_'):
            self._init_latent_vars(n_features)

        if n_features != self.components_.shape[1]:
            raise ValueError(
                "The provided data has %d dimensions while "
                "the model was trained with feature size %d." %
                (n_features, self.components_.shape[1]))

        for idx_slice in gen_batches(n_samples, batch_size):
            self._em_step(X[idx_slice, :], total_samples=self.total_samples, batch_update=False)

        return self

    def fit(self, X, y=None):
        """Learn model for the data X with variational Bayes method.

        When `learning_method` is 'online', use mini-batch update.
        Otherwise, use batch update.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        Returns
        -------
        self
        """
        self._check_params()
        X = self._check_non_neg_array(X, "LatentDirichletAllocation.fit")
        n_samples, n_features = X.shape
        max_iter = self.max_iter
        evaluate_every = self.evaluate_every
        learning_method = self.learning_method
        batch_size = self.batch_size

        # initialize parameters
        self._init_latent_vars(n_features)
        # change to perplexity later
        last_bound = None
        for i in xrange(max_iter):
            if learning_method == 'online':
                for idx_slice in gen_batches(n_samples, batch_size):
                    self._em_step(X[idx_slice, :], total_samples=n_samples, batch_update=False)
            else:
                # batch update
                self._em_step(X, total_samples=n_samples, batch_update=True)

            # check perplexity
            if evaluate_every > 0 and (i + 1) % evaluate_every == 0:
                doc_topics_distr, _ = self._e_step(X, cal_sstats=False, random_init=False)
                bound = self.perplexity(X, doc_topics_distr, sub_sampling=False)
                if self.verbose:
                    print('iteration: %d, perplexity: %.4f' % (i + 1, bound))

                if last_bound and abs(last_bound - bound) < self.perp_tol:
                    break
                last_bound = bound

        return self

    def transform(self, X):
        """Transform data X according to the fitted model.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        Returns
        -------
        doc_topic_distr : shape=(n_samples, n_topics)
            Document topic distribution for X.
        """

        if not hasattr(self, 'components_'):
            raise NotFittedError("no 'components_' attribute in model. Please fit model first.")

        # make sure feature size is the same in fitted model and in X
        X = self._check_non_neg_array(X, "LatentDirichletAllocation.transform")
        n_samples, n_features = X.shape
        if n_features != self.components_.shape[1]:
            raise ValueError(
                "The provided data has %d dimensions while "
                "the model was trained with feature size %d." %
                (n_features, self.components_.shape[1]))

        doc_topic_distr, _ = self._e_step(X, cal_sstats=False, random_init=False)
        return doc_topic_distr

    def _approx_bound(self, X, doc_topic_distr, sub_sampling):
        """Estimate the variational bound.

        Estimate the variational bound over "all documents" using only the
        documents passed in as X. Since log-likelihood of each word cannot
        be computed directly, we use this bound to estimate it.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        doc_topic_distr : array, shape=(n_samples, n_topics)
            Document topic distribution. In the literature, this is called `gamma`.

        sub_sampling : boolean, optional, (default=False)
            Compensate for subsampling of documents.
            It is used in calcuate bound in online learning.

        Returns
        -------
        score : float

        """

        def _loglikelihood(prior, distr, dirichlet_distr, size):
            # calcualte log-likelihood
            score = np.sum((prior - distr) * dirichlet_distr)
            score += np.sum(gammaln(distr) - gammaln(prior))
            score += np.sum(gammaln(prior * size) - gammaln(np.sum(distr, 1)))
            return score

        is_sparse_x = sp.issparse(X)
        n_samples, n_topics = doc_topic_distr.shape
        n_features = self.components_.shape[1]
        score = 0
        dirichlet_doc_topic = _log_dirichlet_expectation(doc_topic_distr)
        doc_topic_prior = self.doc_topic_prior_
        topic_word_prior = self.topic_word_prior_

        if is_sparse_x:
            X_data = X.data
            X_indices = X.indices
            X_indptr = X.indptr

        # E[log p(docs | theta, beta)]
        for idx_d in xrange(0, n_samples):
            if is_sparse_x:
                ids = X_indices[X_indptr[idx_d]:X_indptr[idx_d + 1]]
                cnts = X_data[X_indptr[idx_d]:X_indptr[idx_d + 1]]
            else:
                ids = np.nonzero(X[idx_d, :])[0]
                cnts = X[idx_d, ids]
            temp = dirichlet_doc_topic[idx_d, :, np.newaxis] + self.dirichlet_component_[:, ids]
            tmax = temp.max(axis=0)
            norm_phi = np.log(np.sum(np.exp(temp - tmax), axis=0)) + tmax
            score += np.dot(cnts, norm_phi)

        # compute E[log p(theta | alpha) - log q(theta | gamma)]
        score += _loglikelihood(doc_topic_prior, doc_topic_distr,
                                dirichlet_doc_topic, self.n_topics)

        # Compensate for the subsampling of the population of documents
        if sub_sampling:
            doc_ratio = float(self.total_samples) / n_samples
            score *= doc_ratio

        # E[log p(beta | eta) - log q (beta | lambda)]
        score += _loglikelihood(topic_word_prior, self.components_,
                                self.dirichlet_component_, n_features)

        return score

    def score(self, X, y=None):
        """Calculate approximate log-likelihood as score.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.

        Returns
        -------
        score : float
            Use approximate bound as score.
        """

        X = self._check_non_neg_array(X, "LatentDirichletAllocation.score")

        doc_topic_distr = self.transform(X)
        score = self._approx_bound(X, doc_topic_distr, sub_sampling=False)
        return score

    def perplexity(self, X, doc_topic_distr=None, sub_sampling=False):
        """Calculate approximate perplexity for data X and topic distribution `gamma`.

        Perplexity is defined as exp(-1. * log-likelihood per word)

        Parameters
        ----------
        X : array-like or sparse matrix, [n_samples, n_features]
            Document word matrix.

        doc_topic_distr : None or array, shape=(n_samples, n_topics)
            Document topic distribution.
            If it is None, it will be generated by applying transform on X.

        Returns
        -------
        score : float
            Perplexity score.
        """
        if not hasattr(self, 'components_'):
            raise NotFittedError("no 'components_' attribute in model. Please fit model first.")

        X = self._check_non_neg_array(X, "LatentDirichletAllocation.perplexity")

        if doc_topic_distr is None:
            doc_topic_distr = self.transform(X)
        else:
            n_samples, n_topics = doc_topic_distr.shape
            if n_samples != X.shape[0]:
                raise ValueError("Number of samples in X and doc_topic_distr does not match.")

            if n_topics != self.n_topics:
                raise ValueError("Number of topics does not match.")

        current_samples = X.shape[0]
        bound = self._approx_bound(X, doc_topic_distr, sub_sampling)

        if sub_sampling:
            word_cnt = X.sum() * (float(self.total_samples) / current_samples)
        else:
            word_cnt = X.sum()
        perword_bound = bound / word_cnt

        return np.exp(-1.0 * perword_bound)
