"""`transitforecast` core functionality."""
import matplotlib.pyplot as plt
import numpy as np
import exoplanet as xo
import pymc3 as pm
import theano.tensor as tt
import transitleastsquares as tls
from astropy import units
from scipy.stats import median_absolute_deviation


def build_model(
    lc, pri_t0, pri_p, pri_rprs,
    pri_m_star, pri_m_star_err, pri_r_star, pri_r_star_err,
    tforecast
):
    # Define the model for the light curve
    with pm.Model() as model:
        # Stellar mass
        m_star = pm.Normal(
            'm_star',
            mu=pri_m_star,
            sd=pri_m_star_err
        )

        # Stellar radius
        r_star = pm.Normal(
            'r_star',
            mu=pri_r_star,
            sd=pri_r_star_err
        )

        # Quadratic limb-darkening parameters
        u = xo.distributions.QuadLimbDark(
            'u',
            testval=np.array([0.3, 0.2])
        )

        # Radius ratio
        r = pm.Uniform(
            'r',
            lower=0.,
            upper=1.,
            testval=pri_rprs
        )

        # Impact parameter
        b = xo.distributions.ImpactParameter(
            'b',
            ror=r,
        )

        # Period
        logperiod = pm.Uniform(
            'logperiod',
            lower=-2.3,  # 0.1 d
            upper=3.4,  # 30 d
            testval=np.log(pri_p)
        )
        period = pm.Deterministic('period', tt.exp(logperiod))

        # Mid-transit time
        t0 = pm.Uniform(
            't0',
            lower=lc.time.min(),
            upper=lc.time.max(),
            testval=pri_t0
        )

        # Keplerian orbit
        orbit = xo.orbits.KeplerianOrbit(
            m_star=m_star,
            r_star=r_star,
            period=period,
            t0=t0,
            b=b
        )

        # Model transit light curve
        light_curves = xo.LimbDarkLightCurve(
            u).get_light_curve(orbit=orbit, r=r*r_star, t=lc.time)
        pm.Deterministic('light_curves', light_curves)
        transit_model = pm.math.sum(light_curves, axis=-1)
        transit_model = pm.Deterministic('transit_model', transit_model)

        # The baseline flux
        f0 = pm.Normal(
            'f0',
            mu=np.median(lc.flux),
            sd=median_absolute_deviation(lc.flux)
        )

        # The full model
        lc_model = pm.Deterministic('lc_model', transit_model+f0)

        ########################
        # Forecast transits
        ########################

        texp = np.median(np.diff(tforecast))
        lcforecast = xo.LimbDarkLightCurve(
            u).get_light_curve(orbit=orbit, r=r*r_star, t=tforecast, texp=texp)
        tmforecast = pm.math.sum(lcforecast, axis=-1)
        tmforecast = pm.Deterministic('tmforecast', tmforecast)

        #######################
        # Track some parameters
        #######################

        # Track transit depth
        pm.Deterministic('depth', r**2)

        # Track planet radius (in Earth radii)
        pm.Deterministic(
            'rearth',
            r*r_star*(units.solRad/units.earthRad).si.scale
        )

        # Track semimajor axis (in AU)
        au_per_rsun = (units.solRad/units.AU).si.scale
        pm.Deterministic('a', orbit.a*au_per_rsun)

        # Track system scale
        pm.Deterministic('aRs', orbit.a/r_star)  # normalize by stellar radius

        # Track inclination
        pm.Deterministic('incl', np.rad2deg(orbit.incl))

        # Track transit duration
        # Seager and Mallen-Ornelas (2003) Eq. 3
        sini = np.sin(orbit.incl)
        t14 = (
            (period/np.pi) *
            np.arcsin((r_star/orbit.a*sini) * np.sqrt((1.+r)**2 - b**2))
        )*24.*60.  # min
        t14 = pm.Deterministic('t14', t14)

        # Track stellar density (in cgs units)
        rho_star = pm.Deterministic('rho_star', orbit.rho_star)

        # Track stellar density (in units of solar density)
        rho_sol = (units.solMass/(4./3.*np.pi*units.solRad**3)).cgs.value
        pm.Deterministic('rho_star_sol', orbit.rho_star/rho_sol)

        # Track x2
        x2 = pm.math.sum(((lc.flux-lc_model)/lc.flux_err)**2)
        x2 = pm.Deterministic('x2', x2)

#         # Fit for variance
#         logs2 = pm.Normal('logs2', mu=np.log(np.var(lc.flux)), sd=1)
#         sigma = pm.Deterministic('sigma', pm.math.sqrt(pm.math.exp(logs2)))

        # The likelihood function
        pm.Normal('obs', mu=lc_model, sd=lc.flux_err, observed=lc.flux)

        # Fit for the maximum a posteriori parameters
        map_soln = xo.optimize(start=model.test_point)
        map_soln = xo.optimize(start=map_soln, vars=[f0, period, t0, r])
        map_soln = xo.optimize(start=map_soln, vars=rho_star)
        map_soln = xo.optimize(start=map_soln, vars=t14)
        map_soln = xo.optimize(start=map_soln)

    return model, map_soln


def get_priors_from_tic(tic_id):
    tic_params = tls.catalog_info(TIC_ID=tic_id)
    ld, M_star, M_star_l, M_star_u, R_star, R_star_l, R_star_u = tic_params

    # Guard against bad values in the TIC
    if not np.isfinite(R_star):
        R_star = 1.
    if not np.isfinite(M_star):
        M_star = 1.
    if not np.isfinite(M_star_u):
        M_star_u = 0.1
    if not np.isfinite(M_star_l):
        M_star_l = 0.1
    if not np.isfinite(R_star_u):
        R_star_u = 0.1
    if not np.isfinite(R_star_l):
        R_star_l = 0.1

    # Use stellar parameters from TIC
    pri_m_star = M_star
    pri_m_star_err = (M_star_u+M_star_l)/2.
    pri_r_star = R_star
    pri_r_star_err = (R_star_u+R_star_l)/2.

    return pri_m_star, pri_m_star_err, pri_r_star, pri_r_star_err


# Optimize MAP estimates
def plot_map_soln(lc, map_soln):
    map_p = map_soln['period']
    map_t0 = map_soln['t0']

    fig, axes = plt.subplots(2)

    ax = axes[0]
    ax.plot(lc.time, lc.flux, 'k.', ms=3, mew=0, alpha=0.1)
    ax.plot(lc.time, map_soln['lc_model'], lw=1, color='C0')
    ax.set_xlabel('Time (TBJD)')
    ax.set_ylabel('Normalized Flux')

    ax = axes[1]
    det_flux = lc.flux
    map_phase = ((lc.time-map_t0) % map_p)/map_p
    map_phase[map_phase > 0.5] -= 1
    order = np.argsort(map_phase)
    blc = lc.fold(t0=map_t0, period=map_p).bin(100)
    ax.plot(map_phase, det_flux, 'k.', ms=3, mew=0, alpha=0.1)
    ax.plot(map_phase[order], map_soln['lc_model'][order], color='C0')
    ax.errorbar(
        blc.time, blc.flux, blc.flux_err,
        ls='', marker='o', ms=2, color='k', mfc='white'
        )
    ax.set_xlabel('Phase')
    ax.set_ylabel('Detrended Flux')
    plt.tight_layout()

    return fig, ax


def sample_from_model(model, map_soln):
    with model:
        trace = pm.sample(
            tune=500,
            draws=500,
            start=map_soln,
            chains=8,
            cores=8,
            step=xo.get_dense_nuts_step(target_accept=0.95)
        )
    return trace


def plot_posterior_model(lc, trace):
    varnames = [
        'm_star',
        'r_star',
        'f0',
        'u',
        'r',
        'b',
        'period',
        't0',
        'depth',
        'rearth',
        'a',
        'aRs',
        'incl',
        't14',
        'rho_star',
        'rho_star_sol',
    ]

    # Summary of posteriors
    func_dict = {
        'median': lambda x: np.percentile(x, 50),
        'upper': lambda x: np.percentile(x, 84)-np.percentile(x, 50),
        'lower': lambda x: np.percentile(x, 50)-np.percentile(x, 16),
    }

    summary = pm.summary(
        trace,
        varnames=varnames,
        hdi_prob=0.68,
        stat_funcs=func_dict,
        round_to=8,
    )

    lc_model_med = np.median(trace['lc_model'], axis=0)
    idx = np.random.choice(trace['lc_model'].shape[0], 100)
    lc_model_draws = trace['lc_model'][idx, :]

    post_t0 = summary['median']['t0']
    post_period = summary['median']['period']
    mad = median_absolute_deviation(lc.flux)

    fig, axes = plt.subplots(2)

    ax = axes[0]
    ax.plot(lc.time, lc.flux, 'k.', ms=3, mew=0, alpha=0.5)
    ax.plot(lc.time, (lc_model_draws.T), color='C0', alpha=0.01)
    ax.plot(lc.time, lc_model_med, lw=1.5, color='white')
    ax.plot(lc.time, lc_model_med, lw=1, color='C0')
    ax.set_xlabel('Time (TBJD)')
    ax.set_ylabel('Normalized Flux')

    ax = axes[1]
    det_flux = lc.flux
    phase = ((lc.time-post_t0) % post_period)/post_period
    phase[phase > 0.5] -= 1
    order = np.argsort(phase)
    ax.plot(phase, det_flux, 'k.', ms=3, mew=0, alpha=0.5)
    ax.plot(
        phase[order], (lc_model_draws.T)[order],
        color='C0', alpha=0.01
    )
    ax.plot(
        phase[order], lc_model_med[order],
        color='white', lw=1.5
    )
    ax.plot(
        phase[order], lc_model_med[order],
        color='C0', lw=1
    )
    ax.set_xlim(-0.025, 0.025)
    ax.set_ylim(
        np.min(lc_model_med-3*mad),
        np.max(lc_model_med+3*mad)
    )
    ax.set_xlabel('Phase')
    ax.set_ylabel('Normalized Flux')

    plt.tight_layout()
    return fig, axes


def chi_squared(obs, exp, err):
    x2 = np.sum(((obs-exp)/err)**2, axis=-1)
    return x2


def weighted_percentile(data, weights, percentile):
    cumsum = np.cumsum(weights)
    percentiles = 100*(cumsum-0.5*weights)/cumsum[-1]
    return np.interp(percentile, percentiles, data)


def integrate_tpm(tpm, time, lower_bound, upper_bound):
    idx = np.logical_and(
        time >= lower_bound,
        time <= upper_bound
    )
    return np.trapz(tpm[idx], time[idx])/(upper_bound-lower_bound)