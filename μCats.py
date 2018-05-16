"""
μCats -- a set of routines for detection and analysis of Ca-transients
"""

import os,sys
from numba import jit

from functools import partial
import matplotlib.pyplot as plt


import numpy as np


from numpy import pi
from numpy import linalg
from numpy.linalg import norm
from numpy.random import randn

from scipy.fftpack import dct,idct
from scipy import sparse
from scipy import ndimage



# Requires image-funcut
# find it on github: https://github.com/abrazhe/image-funcut/tree/develop

from imfun.filt.dctsplines import l1spline, l2spline, sp_decompose
from imfun import bwmorph

from imfun import cluster
from imfun.filt.dctsplines import l2spline 
from imfun.core.coords import make_grid
from imfun.multiscale import mvm

from imfun import components


_dtype_ = np.float32


def make_weighting_kern(size,sigma=1.5):
    """
    Make a 2d array of floats to weight signal inputs in the spatial windows/patches
    """
    #size = patch_size_
    x,y = np.mgrid[-size/2.+0.5:size/2.+.5,-size/2.+.5:size/2.+.5]
    g = np.exp(-(0.5*(x/sigma)**2 + 0.5*(y/sigma)**2))
    return g


def smoothed_medianf(v,smooth=10,wmedian=10):
    "Robust smoothing by first applying median filter and then applying L2-spline filter" 
    return l2spline(ndimage.median_filter(v, wmedian),smooth)


def signals_from_array_avg(data, stride=2, patch_size=5):
    """Convert a TXY image stack to a list of temporal signals (taken from small spatial windows/patches)"""  
    d = np.array(data).astype(_dtype_)
    acc = []
    squares =  list(map(tuple, make_grid(d.shape[1:], patch_size,stride)))
    w = make_weighting_kern(patch_size)
    w = w/w.sum()
    
    tslice = (slice(None),)
    for sq in squares:
        patch = d[tslice+sq]
        sh = patch.shape
        wclip = w[:sh[1],:sh[2]]
        #print(w.shape, sh[1:3], wclip.shape)
        #wclip /= sum(wclip)
        signal = (patch*wclip).sum(axis=(1,2))
        acc.append((signal, sq, wclip.reshape(1,-1)))
    return acc
    #signals =  array([d[(slice(None),)+s].sum(-1).sum(-1)/prod(d[0][s].shape) for s in squares])
    #return [(v,sq,w) for v,sq in zip(signals, squares)]



def signals_from_array_pca_cluster(data,stride=2, nhood=5, ncomp=2,
                                   pre_smooth=3,
                                   dbscan_eps=0.05, dbscan_minpts=3, cluster_minsize=5,
                                   walpha=10,
                                   mask_of_interest=None):
    """
    Convert a TXY image stack to a list of signals taken from spatial windows and aggregated according to their coherence
    """
    sh = data.shape
    if mask_of_interest is None:
        mask_of_interest = np.ones(sh[1:],dtype=np.bool)
    mask = mask_of_interest
    counts = np.zeros(sh[1:])
    acc = []
    knn_count = 0
    cluster_count = 0
    Ln = (2*nhood+1)**2
    corrfn=stats.pearsonr
    patch_size = (nhood*2+1)**2
    if cluster_minsize > patch_size:
        cluster_minsize = patch_size
    for r in range(nhood,sh[1]-nhood,stride):
        for c in range(nhood,sh[2]-nhood,stride):
            sys.stderr.write('\r processing location %05d/%d '%(r*sh[1] + c+1, np.prod(sh[1:])))
            if mask[r,c]:
                v = data[:,r,c]
                kcenter = 2*nhood*(nhood+1)
                sl = (slice(r-nhood,r+nhood+1), slice(c-nhood,c+nhood+1))
                patch = data[(slice(None),)+sl]
                if not np.any(patch):
                    continue
                patch = patch.reshape(sh[0],-1).T
                if pre_smooth > 1:
                    patch = ndimage.median_filter(patch, size=(pre_smooth,1))
                Xc = patch.mean(0)
                #Xc =  0
                u,s,vh = np.linalg.svd(patch-Xc,full_matrices=False)
                points = u[:,:ncomp]
                _,_,affs = cluster.dbscan(points, dbscan_eps, dbscan_minpts)
                similar = affs==affs[kcenter]
                dists = cluster.metrics.euclidean(points[kcenter],points)
                if sum(similar) < cluster_minsize or affs[kcenter]==-1:
                    knn_count +=1
                    th = np.argsort(dists)[cluster_minsize+1]
                    similar = dists <= th
                else:
                    cluster_count +=1
                weights = np.exp(-walpha*dists)
                #weights = np.array([corrfn(a,v)[0] for a in patch])**2

                #weights /= np.sum(weights)
                #weights = ones(len(dists))
                weights[~similar] = 0
                #weights = np.array([corrfn(a,v)[0] for a in patch])

                #weights /= np.sum(weights)
                vx = patch[similar].mean(0) # DONE?: weighted aggregate
                                            # TODO: check how weights are defined in NL-Bayes and BM3D
                                            # TODO: project to PCs?
                acc.append((vx, sl, weights))
    return acc


from scipy import stats
def signals_from_array_correlation(data,stride=2,nhood=5,
                                   max_take=10,
                                   corrfn = stats.pearsonr,
                                   mask_of_interest=None):
    """
    Convert a TXY image stack to a list of signals taken from spatial windows and aggregated according to their coherence
    """
    sh = data.shape
    L = sh[0]
    if mask_of_interest is None:
        mask_of_interest = np.ones(sh[1:],dtype=np.bool)
    mask = mask_of_interest
    counts = np.zeros(sh[1:])
    acc = []
    knn_count = 0
    cluster_count = 0
    Ln = (2*nhood+1)**2
    max_take = min(max_take, Ln)
    def _process_loc(r,c):
        v = data[:,r,c]
        kcenter = 2*nhood*(nhood+1)
        sl = (slice(r-nhood,r+nhood+1), slice(c-nhood,c+nhood+1))
        patch = data[(slice(None),)+sl]
        if not np.any(patch):
            return
        patch = patch.reshape(sh[0],-1).T
        weights = np.array([corrfn(a,v)[0] for a in patch])
        weights[weights < 2/L**0.5] = 0 # set weights to 0 in statistically independent sources
        weights[np.argsort(weights)[:-max_take]]=0
        weights = weights/np.sum(weights) # normalize weights
        weights += 1e-6 # add small weight to avoid dividing by zero
        vx = (patch*weights.reshape(-1,1)).sum(0)
        acc.append((vx, sl, weights))
        
        
    for r in range(nhood,sh[1]-nhood,stride):
        for c in range(nhood,sh[2]-nhood,stride):
            sys.stderr.write('\rprocessing location (%03d,%03d), %05d/%d'%(r,c, r*sh[1] + c+1, np.prod(sh[1:])))
            if mask[r,c]:
                _process_loc(r,c)
    for _,sl,w in acc:
        counts[sl] += w.reshape(2*nhood+1,2*nhood+1)
    for r in range(nhood,sh[1]-nhood):
        for c in range(nhood,sh[2]-nhood):
            if mask[r,c] and not counts[r,c]:
                sys.stderr.write('\r (2x) processing location (%03d,%03d), %05d/%d'%(r,c, r*sh[1] + c+1, np.prod(sh[1:])))                
                _process_loc(r,c)
    return acc

from imfun import components
def patch_pca_denoise(data,stride=2, nhood=5, npc=6):
    sh = data.shape
    L = sh[0]
    #if mask_of_interest is None:
    #    mask_of_interest = np.ones(sh[1:],dtype=np.bool)
    out = np.zeros(sh,_dtype_)
    counts = np.zeros(sh[1:],int)
    mask=np.ones(counts.shape,bool)
    Ln = (2*nhood+1)**2
    def _process_loc(r,c):
        sl = (slice(r-nhood,r+nhood+1), slice(c-nhood,c+nhood+1))
        tsl = (slice(None),)+sl
        patch = data[tsl]
        w_sh = patch.shape
        patch = patch.reshape(sh[0],-1).T
        Xc = patch.mean(0)
        Xc = ndimage.median_filter(Xc,3)
        u,s,vh = np.linalg.svd(patch-Xc,full_matrices=False)
        #ux = ndimage.median_filter(u[:,:npc],size=(3,1))
        ux = u[:,:npc]
        proj = ux@np.diag(s[:npc])@vh[:npc]
        out[tsl] += (proj+Xc).T.reshape(w_sh)
        counts[sl] += 1
        
    for r in range(nhood,sh[1]-nhood,stride):
        for c in range(nhood,sh[2]-nhood,stride):
            sys.stderr.write('\rprocessing location (%03d,%03d), %05d/%d'%(r,c, r*sh[1] + c+1, np.prod(sh[1:])))
            if mask[r,c]:
                _process_loc(r,c)
    out = out/counts[None,:,:]
    for r in range(sh[1]):
        for c in range(sh[2]):
            if counts[r,c] ==0:
                out[:,r,c] = 0
    return out

def patch_pca_denoise2(data,stride=2, nhood=5, npc=6,
                       temporal_filter=3,
                       spatial_filter=3,):
    sh = data.shape
    L = sh[0]
    #if mask_of_interest is None:
    #    mask_of_interest = np.ones(sh[1:],dtype=np.bool)
    out = np.zeros(sh,_dtype_)
    counts = np.zeros(sh[1:],_dtype_)
    mask=np.ones(counts.shape,bool)
    Ln = (2*nhood+1)**2
    def _process_loc(r,c):
        sl = (slice(r-nhood,r+nhood+1), slice(c-nhood,c+nhood+1))
        tsl = (slice(None),)+sl
        patch = data[tsl]
        w_sh = patch.shape
        patch = patch.reshape(sh[0],-1)
        # (patch is now Nframes x Npixels, u will hold temporal components)
        u,s,vh = np.linalg.svd(patch,full_matrices=False)
        ux = ndimage.median_filter(u[:,:npc],size=(temporal_filter,1))
        vhx = np.array([ndimage.median_filter(f.reshape(w_sh[1:]),
                                              size=(spatial_filter,spatial_filter))
                        for f in vh[:npc]])
        vhx = vhx.reshape(npc,len(vh[0]))
        #print('\n', patch.shape, u.shape, vh.shape)
        #ux = u[:,:npc]
        proj = ux@np.diag(s[:npc])@vh[:npc]
        score = np.sum(s[:npc]**2)/np.sum(s**2)
        #score = 1
        out[tsl] += score*proj.reshape(w_sh)
        counts[sl] += score
        
    for r in range(nhood,sh[1]-nhood,stride):
        for c in range(nhood,sh[2]-nhood,stride):
            sys.stderr.write('\rprocessing location (%03d,%03d), %05d/%d'%(r,c, r*sh[1] + c+1, np.prod(sh[1:])))
            if mask[r,c]:
                _process_loc(r,c)
    out = out/counts[None,:,:]
    for r in range(sh[1]):
        for c in range(sh[2]):
            if counts[r,c] ==0:
                out[:,r,c] = 0
    return out


def nonlocal_video_smooth(data, stride=2,nhood=5,corrfn = stats.pearsonr,mask_of_interest=None):
    sh = data.shape
    if mask_of_interest is None:
        mask_of_interest = np.ones(sh[1:],dtype=np.bool)
    out = np.zeros(sh,dtype=_dtype_)
    mask = mask_of_interest
    counts = np.zeros(sh[1:])
    acc = []
    knn_count = 0
    cluster_count = 0
    Ln = (2*nhood+1)**2
    for r in range(nhood,sh[1]-nhood,stride):
        for c in range(nhood,sh[2]-nhood,stride):
            sys.stderr.write('\rprocessing location %05d/%d'%(r*sh[1] + c+1, np.prod(sh[1:])))
            if mask[r,c]:
                v = data[:,r,c]
                kcenter = 2*nhood*(nhood+1)
                sl = (slice(r-nhood,r+nhood+1), slice(c-nhood,c+nhood+1))
                patch = data[(slice(None),)+sl]
                w_sh = patch.shape
                patch = patch.reshape(sh[0],-1).T
                weights = np.array([corrfn(a,v)[0] for a in patch])**2
                weights = weights/np.sum(weights)
                wx = weights.reshape(w_sh[1:])
                ks = np.argsort(weights)[::-1]
                xs = ndimage.median_filter(patch, size=(5,1))
                out[(slice(None),)+sl] += xs[np.argsort(ks)].T.reshape(w_sh)*wx[None,:,:]
                counts[sl] += wx
    out /= counts
    
    return out
    

def loc_in_patch(loc,patch):
    sl = patch[1]
    return np.all([s.start <= l < s.stop for l,s in zip(loc, sl)])

def _baseline_windowed_pca(data,stride=4, nhood=7, ncomp=10,
                          smooth = 60,
                          walpha=1.0,
                          mask_of_interest=None):
    sh = data.shape
    if mask_of_interest is None:
        mask_of_interest = np.ones(sh[1:],dtype=np.bool)
    mask = mask_of_interest
    counts = np.zeros(sh[1:])
    acc = []
    knn_count = 0
    cluster_count = 0
    Ln = (2*nhood+1)**2
    out_data = np.zeros(sh,dtype=_dtype_)
    print(out_data.shape)
    counts = np.zeros(sh[1:])
    empty_slice = (slice(None),)

    for r in range(nhood,sh[1]-nhood,stride):
        for c in range(nhood,sh[2]-nhood,stride):
            sys.stderr.write('\rprocessing pixel %05d/%d'%(r*sh[1] + c+1, np.prod(sh[1:])))
            if mask[r,c]:
                kcenter = 2*nhood*(nhood+1)
                sl = (slice(r-nhood,r+nhood+1), slice(c-nhood,c+nhood+1))
                patch = data[(slice(None),)+sl]
                pshape = patch.shape
                patch = patch.reshape(sh[0],-1).T               
                Xc = patch.mean(0)
                u,s,vh = np.linalg.svd(patch-Xc,full_matrices=False)
                points = u[:,:ncomp]
                #pc_signals = array([medismooth(s) for s in points.T])
                pc_signals = np.array([simple_get_baselines(s)  for s in points.T])
                signals = (pc_signals.T@np.diag(s[:ncomp])@vh[:ncomp] + Xc).T
                out_data[empty_slice+sl] += signals.reshape(pshape)
                counts[sl] += np.ones(pshape[1:],dtype=int)
            
    out_data /= (1e-12 + counts)
    return out_data


def combine_weighted_signals(collection,shape):
    """
    Combine a list of processed signals with weights back into TXY frame stack (nframes x nrows x ncolumns)
    """
    out_data = np.zeros(shape,dtype=_dtype_)
    counts = np.zeros(shape[1:])
    tslice = (slice(None),)
    i = 0
    for v,s,w in collection:
        pn = s[0].stop - s[0].start
        #print(s,len(w))
        wx = w.reshape(out_data[tslice+tuple(s)].shape[1:])
        out_data[tslice+tuple(s)] += v.reshape(-1,1,1)*wx
        counts[s] += wx
    out_data /= (1e-12 + counts)
    return out_data




min_px_size_ = 10

# def simple_pipeline(y,tau_label=1.5):
#     ns = rolling_sd_pd(y)
#     vn = y/ns
#     labels = simple_label_lj(vn, tau=tau_label,with_plots=False)
#     return sp_rec_with_labels(y, labels,with_plots=False,)

tau_label_=2.0


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

import pandas as pd
def rolling_sd_pd(v,hw=None,with_plots=False,correct_factor=1.,smooth_output=True,input_is_details=False):
    """
    Etimate time-varying level of noise standard deviation
    """
    if not input_is_details:
        details = v-ndimage.median_filter(v,20)
    else:
        details = v
    if hw is None: hw = int(len(details)/10.)    
    padded = np.pad(details,2*hw,mode='reflect')
    tv = np.arange(len(details))
    
    s = pd.Series(padded)
    rkw = dict(window=2*hw,center=True)

    out = (s - s.rolling(**rkw).median()).abs().rolling(**rkw).median()
    out = 1.4826*np.array(out)[2*hw:-2*hw]
    
    if with_plots:
        f,ax = plt.subplots(1,1,sharex=True)
        ax.plot(tv,details,'gray')
        ax.plot(tv,out,'y')
        ax.plot(tv,2*out,'orange')
        ax.set_xlim(0,len(v))
        ax.set_title('Estimating running s.d.')
        ax.set_xlabel('samples')
    out = out/correct_factor
    if smooth_output:
        out = l2spline(out, s=2*hw)
    return out


def tmvm_baseline(y, plow=25, smooth_level=100, symmetric=False):
    """
    Estimate time-varying baseline in 1D signal by first finding fast significant 
    changes and removing them, followed by smoothing
    """
    rec = process_tmvm(y,k=3,rec_variant=1)
    if symmetric:
        rec_minus = -process_signal(-y,k=3,rec_variant=1)
        rec=rec+rec_minus
    res = y-rec
    b = l2spline(ndimage.percentile_filter(res,plow,smooth_level),smooth_level/4)
    rsd = rolling_sd_pd(res-b)
    return b,rsd,res

def simple_get_baselines(y,th=3,smooth=100,symmetric=False):
    """
    tMVM-based baseline estimation of time-varying baseline with bias correction
    """
    b,ns,res = tmvm_baseline(y,smooth_level=smooth,symmetric=symmetric)
    d = res-b
    return b + np.median(d[d<th*ns]) # + bias as constant shift




from scipy.stats import skew

@jit
def local_jitter(v, sigma=5):
    L = len(v)
    vx = np.copy(v)
    Wvx = np.zeros(L)
    for i in range(L):
        j = i + int(round(randn()*sigma))
        j = max(0,min(j,L-1))
        vx[i] = v[j]
        vx[j] = v[i]
    return vx
    

def std_median(v):
    N = float(len(v))
    md = np.median(v)
    return (np.sum((v-md)**2)/N)**0.5

def mad_std(v):
    mad = np.median(abs(v-np.median(v)))
    return mad*1.4826


def rolling_sd(v,hw=None,with_plots=False,correct_factor=1.,smooth_output=True,input_is_details=False):
    if not input_is_details:
        details = v-ndimage.median_filter(v,20)
    else:
        details = v
    if hw is None: hw = int(len(details)/10.)    
    padded = np.pad(details,hw,mode='reflect')
    tv = np.arange(len(details))
    out = np.zeros(len(details))
    for i in np.arange(len(details)):
        out[i] = mad_std(padded[i:i+2*hw])
    if with_plots:
        f,ax = plt.subplots(1,1,sharex=True)
        ax.plot(tv,details,'gray')
        ax.plot(tv,out,'y')
        ax.plot(tv,2*out,'orange')
        ax.set_xlim(0,len(v))
        ax.set_title('Estimating running s.d.')
        ax.set_xlabel('samples')
    out = out/correct_factor
    if smooth_output:
        out = l2spline(out, s=2*hw)
    return out

def rolling_sd_scipy(v,hw=None,with_plots=False,correct_factor=1.,smooth_output=True,input_is_details=False):
    if not input_is_details:
        details = v-ndimage.median_filter(v,20)
    else:
        details = v
    if hw is None: hw = int(len(details)/10.)    
    padded = np.pad(details,hw,mode='reflect')
    tv = np.arange(len(details))
    #out = np.zeros(len(details))
    
    rolling_median = lambda x: ndimage.median_filter(x, 2*hw)
    
    out = 1.4826*rolling_median(np.abs(padded-rolling_median(padded)))[hw:-hw]

    if with_plots:
        f,ax = plt.subplots(1,1,sharex=True)
        ax.plot(tv,details,'gray')
        ax.plot(tv,out,'y')
        ax.plot(tv,2*out,'orange')
        ax.set_xlim(0,len(v))
        ax.set_title('Estimating running s.d.')
        ax.set_xlabel('samples')
    out = out/correct_factor
    if smooth_output:
        out = l2spline(out, s=2*hw)
    return out


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
            rsd_smoother = lambda v_: ndimage.median_filter(y,7)
        rsd = rolling_sd_pd(y-rsd_smoother(y), input_is_details=True)
    else:
        rsd = np.pad(rsd, npad,"reflect")
    
    #ys = l1spline(y,tau)
    ntau = np.int(np.ceil(tau))
    ys = ndimage.median_filter(y,ntau)
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
        rsd_smoother = lambda v_: ndimage.median_filter(y,7)
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
    

# Labeling algorithms

def simple_label(v, threshold=1.0,tau=5., smoother=l2spline,**kwargs):
    vs = smoother(v, tau)
    return vs >= threshold

def percentile_label(v, percentile_low=5.0,tau=1.0,smoother=l2spline):
    mu = min(np.median(v),0)
    low = np.percentile(v[v<mu], percentile_low)
    vs = smoother(v, tau)
    return vs >= -low
    

def with_local_jittering(labeler, niters=100, weight_thresh=0.85):
    def _(v, *args, **kwargs):
        if 'tau' in kwargs:
            tau = kwargs['tau']
        else:
            tau = 5.0
        labels_history = np.zeros((niters,len(v)))
        for i_ in range(niters):
            vi = local_jitter(v,0.5*tau)
            #labels_history.append(labeler(vi, *args, **kwargs))
            labels_history[i_] =labeler(vi, *args, **kwargs)
        return np.mean(labels_history,0) >= weight_thresh
    return _

simple_label_lj = with_local_jittering(simple_label)
percentile_label_lj = with_local_jittering(percentile_label)

thresholds_l1 = np.array([2.26212451,  1.11505896,  0.52321721,  0.51701626,  0.42481402,
                          0.34870014,  0.29144794,  0.24410656,  0.20409004,  0.16792375,
                          0.13579082,  0.10770976])
thresholds_l1 = thresholds_l1.reshape(-1,1)


thresholds_l2 = np.array([ 1.6452271 ,  0.64617428,  0.41641932,  0.32425908,  0.26115802,
                           0.21203462,  0.17222229,  0.14062114,  0.11350558,  0.0896438 ,
                           0.06936852,  0.05300952])
thresholds_l2 = thresholds_l2.reshape(-1,1)



def multiscale_labeler_l1(signal,thresh=2,start=1,**kwargs):
    coefs = sp_decompose(signal, level=12, smoother=l1spline,base=1.5)[start:-1]
    labels = (coefs>=thresholds_l1[start:]).sum(axis=0)>=thresh
    return labels
    
def multiscale_labeler_l2(signal,thresh=4,start=1,**kwargs):
    #thresholds_l2 = array([1.6453141 , 0.64634246, 0.41638476, 0.3242796 , 0.2611729 ,
    #                       0.21204839, 0.17224974, 0.14053809, 0.11334435, 0.08955742,
    #                       0.06948411, 0.05307127]).reshape(-1,1)
    coefs = sp_decompose(signal, level=12, smoother=l2spline,base=1.5)[start:-1]
    labels = (coefs>=thresholds_l2[start:]).sum(axis=0)>=thresh
    return labels

def make_labeler_commitee(*labelers):
    Nl = len(labelers)
    def _(v,**kwargs):
        labels = [lf(v) for lf in labelers]
        return np.sum(labels,0)==Nl
    return _

multiscale_labeler_l1l2 = make_labeler_commitee(multiscale_labeler_l1, 
                                               partial(multiscale_labeler_l2, start=2,thresh=3))

multiscale_labeler_joint = make_labeler_commitee(multiscale_labeler_l1, 
                                                 partial(multiscale_labeler_l2, start=2,thresh=3),
                                                 simple_label_lj)


# Reconstruction

from imfun import bwmorph


def sp_rec_with_labels(vec, labels, 
                       min_scale=1.0,max_scale=50.,
                       with_plots=True,
                       min_size=3,
                       niters=10,
                       kgain=0.25,
                       smoother=smoothed_medianf,
                       wmedian=3,
                       return_smoothed=False):
    if min_size > 1:
        regions = bwmorph.contiguous_regions(labels)
        regions = bwmorph.filter_size_regions(regions, min_size)
        filtered_labels = np.zeros_like(labels)+np.sum([r.tomask() for r in regions],axis=0)
    else:
        filtered_labels=labels
    
    if not sum(filtered_labels):
        return np.zeros_like(vec)
    vec1 = np.copy(vec)

    
    
    vs = smoother(vec1, min_scale, wmedian)
    weights = np.clip(labels, 0,1)
    
    #vss = smoother(vec-vs,max_scale,weights=weights<0.9)

    vrec = smoother(vs*(vec1>0),min_scale,wmedian)
    
    for i in range(niters):
        vec1 = vec1 - kgain*(vec1-vrec)
        labs,nl = ndimage.label(weights)
        objs = ndimage.find_objects(labs)
        for o in objs:
            stop = o[0].stop
            while stop < len(vec) and vrec[stop]>0.1:
                weights[stop] = 1   
                stop+=1
        wer = ndimage.binary_erosion(weights)
        weights = np.where((vec1<0.5), wer, weights)
        vrec = smoother(vs*weights,min_scale)
        #weights = ndimage.binary_opening(weights)
        vrec[vrec<0] = 0
        #vrec[weights<0.5] *=0.5
        
    
    if with_plots:
        f,ax = plt.subplots(1,1)
        ax.plot(vec, '-',ms=2, color='gray',lw=0.5,alpha=0.5)
        ax.plot(vec1, '-', color='cyan',lw=0.75,alpha=0.75)
        ax.plot(weights, 'g',lw=2,alpha=0.5)
        ax.plot(vs, color='k',alpha=0.5)
        #plot(vss,color='navy',alpha=0.5)
        ax.plot(vrec, color='royalblue',lw=2)
        ll = np.where(labels)[0]
        ax.plot(ll,-1*np.ones_like(ll),'r|')
    if return_smoothed:
        return vrec
    else:
        return weights*(vec>0)*vec
        

def simple_pipeline_(y, labeler=percentile_label,labeler_kw=None):
    """
    Detect and reconstruct Ca-transients in 1D signal
    """
    if not any(y):
        return np.zeros_like(y)
    ns = rolling_sd_pd(y)
    low = y < 2.5*np.median(y)
    if not any(low):
        low = np.ones(len(y),np.bool)
    bias = np.median(y[low])
    if bias > 0:
        y = y-bias    
    vn = y/ns
    #labels = simple_label_lj(vn, tau=tau_label_,with_plots=False)
    if labeler_kw is None:
        labeler_kw={}
    labels = labeler(vn, **labeler_kw)
    if not any(labels):
        return np.zeros_like(y)
    return sp_rec_with_labels(vn, labels,with_plots=False)*ns

def simple_pipeline_nojitter_(y,tau_label=1.5):
    """
    Detect and reconstruct Ca-transients in 1D signal
    """
    ns = rolling_sd_pd(y)
    low = y < 2.5*np.median(y)
    if not any(low):
        low = np.ones(len(y),np.bool)
    bias = np.median(y[low])
    if bias > 0:
        y = y-bias    
    vn = y/ns
    labels = simple_label(vn, tau=tau_label,with_plots=False)
    return y * labels
    #return sp_rec_with_labels(vn, labels,niters=5,with_plots=False)*ns


def simple_pipeline_with_baseline(y,tau_label=1.5):
    """
    Detect and reconstruct Ca-transients in 1D signal after normalizing to baseline
    """    
    b,ns,_ = tmvm_baseline(y)
    b = b + np.median(y-b)
    vn = (y-b)/ns
    labels = simple_label_lj(vn, tau=tau_label,with_plots=False)
    rec = sp_rec_with_labels(y, labels,with_plots=False,)
    return where(b>0,rec,0)

from multiprocessing import Pool
def process_signals_parallel(collection, pipeline=simple_pipeline_,njobs=4):
    """
    Process temporal signals some pipeline function and return processed signals
    (parallel version)
    """
    out =[]
    pool = Pool(njobs)
    recs = pool.map(pipeline, [c[0] for c in collection], chunksize=4) # setting chunksize here is experimental
    pool.close()
    pool.join()    
    return [(r,s,w) for r,(v,s,w) in zip(recs, collection)]


def quantify_events(rec, labeled,dt=1):
    "Collect information about transients for a 1D reconstruction"
    acc = []
    idx = np.arange(len(rec))
    for i in range(1,np.max(labeled)+1):
        mask = labeled==i
        cut = rec[mask],
        ev = dict(
            start = np.min(idx[mask]),
            stop = np.max(idx[mask]),
            peak = np.max(cut),
            time_to_peak = np.argmax(cut),
            vmean = np.mean(cut))
        acc.append(ev)
    return acc

from imfun.core import extrema

def segment_events(rec, th=0.25):
    levels = rec>th
    labeled, nlab = ndimage.label(levels)
    smrec = l1spline(rec, 6)
    #smrec = l2spline(rec, 6)
    mxs = np.array(extrema.locextr(smrec, output='max',refine=False),int)
    mns = np.array(extrema.locextr(smrec, output='min',refine=False),int)
    if not len(mxs) or not len(mns) or not np.any(mxs[:,1]>1):
        return labeled, nlab
    mxs = mxs[mxs[:,1]>1]
    cuts = []
    
    for i in range(1,nlab+1):
        mask = labeled==i
        lmax = [m for m in mxs if mask[m[0]]]
        if len(lmax)>1:
            th = np.min([m[1] for m in lmax])*0.75
            lms = [mn for mn in mns if mask[mn[0]] and mn[1]<th]
            if len(lms):
                #print "Possibly batched",i
                for lm in lms:
                    cuts.append(lm[0])
                    levels[lm[0]]=False
    labeled,nlab=ndimage.label(levels)
    #plot(labeled>0)
    return labeled, nlab
    #    plot(rec*(labeled==i),alpha=0.5)


def pca_flip_signs(pcf,medianw=None):
    L = len(pcf.coords)
    if medianw is None:
        medianw = L//5
    for i,c in enumerate(pcf.coords.T):
        sk = skew(c-ndimage.median_filter(c,medianw))
        sg = np.sign(sk)
        #print(i, sk)
        pcf.coords[:,i] *= sg
        pcf.tsvd.components_[:,i]*=sg
    return pcf

def svd_flip_signs(u,vh,medianw=None):
    L = len(u)
    if medianw is None:
        medianw = L//5
    for i,c in enumerate(u.T):
        sk = skew(c-ndimage.median_filter(c,medianw))
        sg = np.sign(sk)
        u[:,i] *= sg
        vh[i]*=sg
    return u,vh


def calculate_baseline(frames,pipeline=simple_get_baselines, stride=2,patch_size=5,return_type='array'):
    """
    Given a TXY frame timestack estimate slowly-varying baseline level of fluorescence using patch-based processing
    """
    from imfun import fseq
    collection = signals_from_array_avg(frames,stride=stride, patch_size=patch_size)
    recsb = process_signals_parallel(collection, pipeline=pipeline, njobs=4)
    sh = frames.shape
    out =  combine_weighted_signals(recsb, sh)
    if return_type.lower() == 'array':
        return out
    fsx = fseq.from_array(out)
    fsx.meta['channel'] = 'baseline'
    return fsx


def calculate_baseline_pca(frames,smooth=60,npc=None,pcf=None,return_type='array',smooth_fn=baseline_als_spl):
    """Use smoothed principal components to estimate time-varying baseline fluorescence F0"""
    from imfun import fseq

    if pcf is None:
        if npc is None:
            npc = len(frames)//20
        pcf = components.pca.PCA_frames(frames,npc=npc)
    pca_flip_signs(pcf)
    #base_coords = np.array([smoothed_medianf(v, smooth=smooth1,wmedian=smooth2) for v in pcf.coords.T]).T    
    base_coords = np.array([smooth_fn(v,smooth=smooth) for v in pcf.coords.T]).T
    #base_coords = np.array([double_scale_baseline(v,smooth1=smooth1,smooth2=smooth2) for v in pcf.coords.T]).T
    #base_coords = np.array([simple_get_baselines(v) for v in pcf.coords.T]).T
    baseline_frames = pcf.tsvd.inverse_transform(base_coords).reshape(len(pcf.coords),*pcf.sh) + pcf.mean_frame
    if return_type.lower() == 'array':
        return baseline_frames
    #baseline_frames = base_coords.dot(pcf.vh).reshape(len(pcf.coords),*pcf.sh) + pcf.mean_frame
    fs_base = fseq.from_array(baseline_frames)
    fs_base.meta['channel'] = 'baseline_pca'
    return fs_base

def get_baseline_frames(frames,smooth=60,npc=None):
    """
    Given a TXY frame timestack estimate slowly-varying baseline level of fluorescence, two-stage processing
    (1) global trends via PCA
    (2) local corrections by patch-based algorithm
    """
    from imfun import fseq
    base1 = calculate_baseline_pca(frames,smooth=smooth,npc=npc)
    base2 = calculate_baseline(frames-base1)
    fs_base = fseq.from_array(base1+base2)
    fs_base.meta['channel']='baseline_comb'
    return fs_base
    
from imfun.core import coords
from numpy.linalg import svd
from multiprocessing import Pool
def map_patches(fn, data,patch_size=10,stride=1,tslice=slice(None),njobs=1):
    """
    Apply some function to a square patch exscized from video
    """
    sh = data.shape[1:]
    squares = list(map(tuple, coords.make_grid(sh, patch_size, stride)))
    if njobs>1:
        pool = Pool(njobs)
        expl_m = pool.map(fn, (data[(tslice,) + s] for s in squares))
    else:
        expl_m = [fn(data[(tslice,) + s]) for s in squares]
    out = np.zeros(sh); 
    counts = np.zeros(sh);
    for _e, s in zip(expl_m, squares):
        out[s] += _e; counts[s] +=1.
    return out/counts

from imfun.core import extrema
def roticity_fft(data,period_low = 100, period_high=5,npc=6):
    """
    Look for local areas with oscillatory dynamics in TXY framestack
    """
    L = len(data)
    if ndim(data)>2:
        data = data.reshape(L,-1)
    Xc = data.mean(0)
    data = data-Xc
    npc = min(npc, data.shape[-1])
    u,s,vh = svd(data,full_matrices=False)
    s2 = s**2/(s**2).sum()
    u = (u-u.mean(0))[:,:npc]
    p = (abs(fft.fft(u,axis=0))**2)[:L//2]
    nu = fft.fftfreq(len(data))[:L//2]
    nu_phys = (nu>1/period_low)*(nu<period_high)
    peak = 0
    sum_peak = 0
    for i in range(npc):
        lm = np.array(extrema.locextr(p[:,i],x=nu,refine=True,output='max'))
        lm = lm[(lm[:,0]>1/period_low)*(lm[:,0]<1/period_high)]
        peak_ = np.amax(lm[:,1])/p[:,i][~nu_phys].mean()*s2[i]
        #print(amax(lm[:,1]),std(p[:,i]),peak_)
        sum_peak += peak_
        peak = max(peak, peak_)
    return sum_peak


def make_enh4(frames,pipeline=simple_pipeline_,kind='pca',nhood=5,stride=2):
    from imfun import fseq
    #coll = ucats.signals_from_array_pca_cluster(frames,stride=2,dbscan_eps=0.05,nhood=5,walpha=0.5)
    if kind.lower()=='corr':
        coll = signals_from_array_correlation(frames,stride=stride,nhood=nhood)
    elif kind.lower()=='pca':
        coll = signals_from_array_pca_cluster(frames,stride=stride,nhood=nhood)
    else:
        coll = signals_from_array_avg(frames,stride=stride,patch_size=nhood*2+1)
    print('\nTime-signals, grouped,  processing (may take long time) ...')
    coll_enh = process_signals_parallel(coll,pipeline=pipeline)
    print('Time-signals processed, recombining to video...')
    out = combine_weighted_signals(coll_enh,frames.shape)
    fsx = fseq.from_array(out)
    print('Done')
    fsx.meta['channel']='-'.join(['newrec4',kind])
    return fsx

def process_framestack(frames,min_area=16,verbose=True):
    """
    Default pipeline to process a stack of frames containing Ca fluorescence to find astrocytic Ca events
    Input: F(t): temporal stack of frames (Nframes x Nx x Ny)
    Output: Collection of two frame stacks containting ΔF/F0 signals, one thresholded and one denoised, and a baseline F0(t): 
            fseq.FStackColl([fsx, dfof_filtered, F0])
    """
    from imfun import fseq
    if verbose:
        print('calculating baseline F0(t)')
    fs_f0 = get_baseline_frames(frames[:])
    fs_f0.meta['channel'] = 'F0'

    dfof= frames/fs_f0.data - 1

    if verbose:
        print('filtering ΔF/F0 data')
    dfof_cleaned = patch_pca_denoise2(dfof, spatial_filter=5, npc=5)
    fs_dfof = fseq.from_array(dfof_cleaned)
    fs_dfof.meta['channel'] = 'ΔF/F0 filtered'
    
    if verbose:
        print('detecting events')
    fsx = make_enh4(dfof_cleaned,nhood=2,kind='pca')
    coll_ = EventCollection(fsx.data,min_area=min_area)
    meta = fsx.meta
    fsx = fseq.from_array(fsx.data*(coll_.to_filtered_array()>0),meta=meta)
    fscoll = fseq.FStackColl([fsx, fs_dfof, fs_f0])
    return fscoll
    

def segment_events(dataset,threshold=0.01):
    labels, nlab = ndimage.label(np.asarray(dataset,dtype=_dtype_)>threshold)
    objs = ndimage.find_objects(labels)
    return labels, objs


class EventCollection:
    def __init__(self, frames, threshold=0.025,min_duration=3,min_area=9):
        self.min_duration = min_duration
        self.labels, self.objs = segment_events(frames,threshold)
        self.coll = [dict(duration=self.event_duration(k),
                          area = self.event_area(k),
                          volume = self.event_volume(k),
                          peak = self.data_value(k,frames),
                          avg = self.data_value(k,frames,np.mean),
                          start=self.objs[k][0].start,
                          idx=k)
                    for k in range(len(self.objs))]
        self.filtered_coll = [c for c in self.coll
                              if c['duration']>min_duration \
                              and c['peak']>0.05\
                              and c['area']>min_area]
    def event_duration(self,k):
        o = self.objs[k]
        return o[0].stop-o[0].start
    def event_volume_mask(self,k):
        return self.labels[self.objs[k]]==k+1
    def project_event_mask(self,k):
        return np.max(self.event_volume_mask(k),axis=0)
    def event_area(self,k):
        return np.sum(self.project_event_mask(k).astype(int))
    def event_volume(self,k):
        return np.sum(self.event_volume_mask(k))
    def data_value(self,k,data,fn = np.max):
        o = self.objs[k]
        return fn(data[o][self.event_volume_mask(k)])
    def to_csv(self,name):
        df = pd.DataFrame(self.filtered_coll)
        df.to_csv(name)
    def to_filtered_array(self):
        sh  = self.labels.shape
        out = np.zeros(sh,dtype=np.int)
        for d in self.filtered_coll:
            k = d['idx']
            o = self.objs[k]
            cond = self.labels[o]==k+1
            out[o][cond] = k
        return out
