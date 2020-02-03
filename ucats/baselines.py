"""
baselines -- routines to estimate baseline fluorescence in 1D or TXY data
"""


import pickle

from functools import partial

import numpy as np
from numpy import linalg
from numpy.linalg import norm, lstsq, svd, eig

from scipy import ndimage as ndi

from numba import jit


import matplotlib.pyplot as plt


from imfun import fseq
from imfun.filt.dctsplines import l2spline
from imfun import components
from imfun.multiscale import mvm



min_px_size_ = 10

from .patches import make_grid
from .utils import rolling_sd_pd, find_bias, find_bias_frames
from .utils import process_signals_parallel
from .decomposition  import pca_flip_signs
from . import patches
from .globals import _dtype_


def store_baseline_pickle(name, frames, ncomp=50):
    pcf = components.pca.PCA_frames(frames,npc=50)
    pickle.dump(pcf, open(name, 'wb'))

def load_baseline_pickle(name):
    pcf = pickle.load(open(name, 'rb'))
    return pcf.inverse_transform(pcf.coords)



@jit
def running_min(v):
    out = np.zeros(v.shape)
    mn = v[0]
    for i in range(len(v)):
        mn = min(mn, v[i])
        out[i] = mn
    return out

def top_running_min(v):
    return np.maximum(running_min(v), running_min(v[::-1])[::-1])


from skimage.restoration import denoise_tv_chambolle
def baseline_runmin(y, smooth=50, wsize=50, wstep=5):
    L = len(y)
    sqs = make_grid((L,), wsize,wstep)
    counts = np.zeros(L)
    out = np.zeros(L)
    for sq in sqs:
        sl = sq[0]
        bx = top_running_min(y[sl])
        out[sl] += bx
        counts[sl] += 1
    return denoise_tv_chambolle(out/(counts+1e-5), weight=smooth)

def process_tmvm(v, k=3,level=7, start_scale=1, tau_smooth=1.5,rec_variant=2,nonnegative=True):
    """
    Process temporal signal using MVM and return reconstructions of significant fluorescence elevations
    """
    objs = mvm.find_objects(v, k=k, level=level, min_px_size=min_px_size_,
                            min_nscales=3,
                            modulus=not nonnegative,
                            rec_variant=rec_variant,
                            start_scale=start_scale)
    if len(objs):
        if nonnegative:
            r = np.max(list(map(mvm.embedded_to_full, objs)),0).astype(v.dtype)
        else:
            r = np.sum([mvm.embedded_to_full(o) for o in objs],0).astype(v.dtype)
        if tau_smooth>0:
            r = l2spline(r, tau_smooth)
        if nonnegative:
            r[r<0]=0
    else:
        r = np.zeros_like(v)
    return r


def tmvm_baseline(y, plow=25, smooth_level=100, symmetric=False):
    """
    Estimate time-varying baseline in 1D signal by first finding fast significant
    changes and removing them, followed by smoothing
    """
    rec = process_tmvm(y,k=3,rec_variant=1)
    if symmetric:
        rec_minus = -process_tmvm(-y,k=3,rec_variant=1)
        rec=rec+rec_minus
    res = y-rec
    b = l2spline(ndi.percentile_filter(res,plow,smooth_level),smooth_level/2)
    rsd = rolling_sd_pd(res-b)
    return b,rsd,res

def tmvm_get_baselines(y,th=3,smooth=100,symmetric=False):
    """
    tMVM-based baseline estimation of time-varying baseline with bias correction
    """
    b,ns,res = tmvm_baseline(y,smooth_level=smooth,symmetric=symmetric)
    d = res-b
    return b + np.median(d[d<=th*ns]) # + bias as constant shift


def simple_baseline(y, plow=25, th=3, smooth=25,ns=None):
    b = l2spline(ndi.percentile_filter(y,plow,smooth),smooth/5)
    if ns is None:
        ns = rolling_sd_pd(y)
    d = y-b
    if not np.any(ns):
        ns = np.std(y)
    bg_points = d[np.abs(d)<=th*ns]
    if len(bg_points) > 10:
        b = b + np.median(bg_points) # correct scalar shift
    return b

def multi_scale_simple_baseline(y, plow=50, th=3, smooth_levels=[10,20,40,80,160], ns=None):
    if ns is None:
        ns = rolling_sd_pd(y)
    b_estimates = [simple_baseline(y,plow,th,smooth,ns) for smooth in smooth_levels]
    low_env = np.amin(b_estimates, axis=0)
    low_env = np.clip(low_env,np.min(y), np.max(y))
    return  l2spline(low_env, np.min(smooth_levels))


def baseline_als_spl(y, k=0.5, tau=11, smooth=25., p=0.001, niter=100,eps=1e-4,
                 rsd = None,
                 rsd_smoother = None,
                 smoother = l2spline,
                 asymm_ratio = 0.9, correct_skew=False):
    """Implements an Asymmetric Least Squares Smoothing
    baseline correction algorithm (P. Eilers, H. Boelens 2005),
    via DCT-based spline smoothing
    """
    #npad=int(smooth)
    nsmooth = np.int(np.ceil(smooth))
    npad =nsmooth

    y = np.pad(y,npad,"reflect")
    L = len(y)
    w = np.ones(L)

    if rsd is None:
        if rsd_smoother is None:
            #rsd_smoother = lambda v_: l2spline(v_, 5)
            #rsd_smoother = lambda v_: ndi.median_filter(y,7)
            rsd_smoother = partial(ndi.median_filter, size=7)
        rsd = rolling_sd_pd(y-rsd_smoother(y), input_is_details=True)
    else:
        rsd = np.pad(rsd, npad,"reflect")

    #ys = l1spline(y,tau)
    ntau = np.int(np.ceil(tau))
    ys = ndi.median_filter(y,ntau)
    s2 = l1spline(y, smooth/4.)
    #s2 = l2spline(y,smooth/4.)
    zprev = None
    for i in range(niter):
        z = smoother(ys,s=smooth,weights=w)
        clip_symm = abs(y-z) > k*rsd
        clip_asymm = y-z > k*rsd
        clip_asymm2 = y-z <= -k*rsd
        r = asymm_ratio#*core.rescale(1./(1e-6+rsd))

        #w = p*clip_asymm + (1-p)*(1-r)*(~clip_symm) + (1-p)*r*(clip_asymm2)
        w = p*(1-r)*clip_asymm + (1-p)*(~clip_symm) + p*r*(clip_asymm2)
        w[:npad] = (1-p)
        w[-npad:] = (1-p)
        if zprev is not None:
            if norm(z-zprev)/norm(zprev) < eps:
                break
        zprev=z
    z = smoother(np.min((z, s2),0),smooth)
    if correct_skew:
        # Correction for skewness introduced by asymmetry.
        z += r*rsd
    return z[npad:-npad]

def double_scale_baseline(y,smooth1=15.,smooth2=25.,rsd=None,**kwargs):
    """
    Baseline estimation in 1D signals by asymmetric smoothing and using two different time scales
    """
    if rsd is None:
        #rsd_smoother = lambda v_: ndi.median_filter(y,7)
        rsd_smoother = partial(ndi.median_filter, size=7)
        rsd = rolling_sd_pd(y-rsd_smoother(y), input_is_details=True)
    b1 = baseline_als_spl(y,tau=smooth1,smooth=smooth1,rsd=rsd,**kwargs)
    b2 = baseline_als_spl(y,tau=smooth1,smooth=smooth2,rsd=rsd,**kwargs)
    return l2spline(np.amin([b1,b2],0),smooth1)


def viz_baseline(v,dt=1.,baseline_fn=baseline_als_spl,
                 smoother=partial(l2spline,s=5),ax=None,**kwargs):
    """
    Visualize results of baseline estimation
    """
    if ax is None:
        plt.figure(figsize=(14,6))
        ax = plt.gca()
    tv = np.arange(len(v))*dt
    ax.plot(tv,v,'gray')
    b = baseline_fn(v,**kwargs)
    rsd = rolling_sd_pd(v-smoother(v))
    ax.fill_between(tv, b-rsd,b+rsd, color='y',alpha=0.75)
    ax.fill_between(tv, b-2.0*rsd,b+2.0*rsd, color='y',alpha=0.5)
    ax.plot(tv,smoother(v),'k')
    ax.plot(tv,b, 'teal',lw=1)
    ax.axis('tight')

def calculate_baseline(frames,pipeline=multi_scale_simple_baseline, stride=2,patch_size=5,return_type='array',
                       pipeline_kw=None):
    """
    Given a TXY frame timestack estimate slowly-varying baseline level of fluorescence using patch-based processing
    """
    from imfun import fseq
    collection = patches.signals_from_array_avg(frames,stride=stride, patch_size=patch_size)
    recsb = process_signals_parallel(collection, pipeline=pipeline, pipeline_kw=pipeline_kw,njobs=4, )
    sh = frames.shape
    out =  patches.combine_weighted_signals(recsb, sh)
    if return_type.lower() == 'array':
        return out
    fsx = fseq.from_array(out)
    fsx.meta['channel'] = 'baseline'
    return fsx

def calculate_baseline_pca(frames,smooth=60,npc=None,pcf=None,return_type='array',smooth_fn=baseline_als_spl):
    """Use smoothed principal components to estimate time-varying baseline fluorescence F0
    -- deprecated
"""
    from imfun import fseq

    if pcf is None:
        if npc is None:
            npc = len(frames)//20
        pcf = components.pca.PCA_frames(frames,npc=npc)
    pca_flip_signs(pcf)
    #base_coords = np.array([smoothed_medianf(v, smooth=smooth1,wmedian=smooth2) for v in pcf.coords.T]).T
    if smooth > 0:
        base_coords = np.array([smooth_fn(v,smooth=smooth) for v in pcf.coords.T]).T
        #base_coords = np.array([multi_scale_simple_baseline(v) for v in pcf.coords.T]).T
    else:
        base_coords = pcf.coords
    #base_coords = np.array([double_scale_baseline(v,smooth1=smooth1,smooth2=smooth2) for v in pcf.coords.T]).T
    #base_coords = np.array([simple_get_baselines(v) for v in pcf.coords.T]).T
    baseline_frames = pcf.tsvd.inverse_transform(base_coords).reshape(len(pcf.coords),*pcf.sh) + pcf.mean_frame
    if return_type.lower() == 'array':
        return baseline_frames
    #baseline_frames = base_coords.dot(pcf.vh).reshape(len(pcf.coords),*pcf.sh) + pcf.mean_frame
    fs_base = fseq.from_array(baseline_frames)
    fs_base.meta['channel'] = 'baseline_pca'
    return fs_base

def calculate_baseline_pca_asym(frames,niter=50,ncomp=20,smooth=25,th=1.5,verbose=False):
    """Use asymetrically smoothed principal components to estimate time-varying baseline fluorescence F0"""
    frames_w = np.copy(frames)
    sh = frames.shape
    nbase = np.linalg.norm(frames)
    diff_prev = np.linalg.norm(frames_w)/nbase
    for i in range(niter+1):
        pcf = components.pca.PCA_frames(frames_w, npc=ncomp)
        coefs = np.array([l2spline(v,smooth) for v in pcf.coords.T]).T
        rec = pcf.inverse_transform(coefs)
        diff_new = np.linalg.norm(frames_w - rec)/nbase
        epsx = diff_new-diff_prev
        diff_prev = diff_new

        if not i%5:
            if verbose:
                sys.stdout.write('%0.1f %% | '%(100*i/niter))
                print('explained variance %:', 100*pcf.tsvd.explained_variance_ratio_.sum(), 'update: ', epsx)
        if i < niter:
            delta=frames_w-rec
            thv = th*np.std(delta,axis=0)
            frames_w = np.where(delta>thv, rec, frames_w)
        else:
            if verbose:
                print('\n finished iterations')
            delta = frames-rec
            #ns0 = np.median(np.abs(delta - np.median(delta,axis=0)), axis=0)*1.4826
            ns0 = mad_std(delta, axis=0)
            biases = find_bias_frames(delta,3,ns0)
            biases[np.isnan(biases)] = 0
            frames_w = rec + biases#np.array([find_bias(delta[k],ns=ns0[k]) for k,v in enumerate(rec)])[:,None]

    return frames_w

## TODO use NMF or NNDSVD instead of PCA?
from sklearn import decomposition as skd
from imfun import core
def _calculate_baseline_nmf(frames, ncomp=None, return_type='array',smooth_fn=multi_scale_simple_baseline):
    """DOESNT WORK! Use smoothed NMF components to estimate time-varying baseline fluorescence F0"""
    from imfun import fseq

    fsh = frames[0].shape

    if ncomp is None:
        ncomp = len(frames)//20
    nmfx = skd.NMF(ncomp,)
    signals = nmfx.fit_transform(core.ah.ravel_frames(frames))

    #base_coords = np.array([smoothed_medianf(v, smooth=smooth1,wmedian=smooth2) for v in pcf.coords.T]).T
    if smooth > 0:
        base_coords = np.array([smooth_fn(v,smooth=smooth) for v in pcf.coords.T]).T
        #base_coords = np.array([multi_scale_simple_baseline for v in pcf.coords.T]).T
    else:
        base_coords = pcf.coords
    #base_coords = np.array([double_scale_baseline(v,smooth1=smooth1,smooth2=smooth2) for v in pcf.coords.T]).T
    #base_coords = np.array([simple_get_baselines(v) for v in pcf.coords.T]).T
    baseline_frames = pcf.tsvd.inverse_transform(base_coords).reshape(len(pcf.coords),*pcf.sh) + pcf.mean_frame
    if return_type.lower() == 'array':
        return baseline_frames
    #baseline_frames = base_coords.dot(pcf.vh).reshape(len(pcf.coords),*pcf.sh) + pcf.mean_frame
    fs_base = fseq.from_array(baseline_frames)
    fs_base.meta['channel'] = 'baseline_pca'
    return fs_base


def get_baseline_frames(frames,smooth=60,npc=None,baseline_fn=multi_scale_simple_baseline,baseline_kw=None):
    """
    Given a TXY frame timestack estimate slowly-varying baseline level of fluorescence, two-stage processing
    (1) global trends via PCA
    (2) local corrections by patch-based algorithm
    """
    from imfun import fseq
    base1 = calculate_baseline_pca(frames,smooth=smooth,npc=npc,smooth_fn=multi_scale_simple_baseline)
    base2 = calculate_baseline(frames-base1, pipeline=baseline_fn, pipeline_kw=baseline_kw,patch_size=5)
    fs_base = fseq.from_array(base1+base2)
    fs_base.meta['channel']='baseline_comb'
    return fs_base