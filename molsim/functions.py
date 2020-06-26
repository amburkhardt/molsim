import numpy as np
from molsim.constants import h, k, ckm
from molsim.classes import Spectrum
from molsim.utils import find_limits, _get_res, _find_nans, find_peaks, find_nearest
from molsim.stats import get_rms

def sum_spectra(sims,thin=True,Tex=None,Tbg=None,res=None,name='sum'):

	'''
	Adds all the spectra in the simulations list and returns a spectrum object.  By default,
	it assumes the emission is optically thin and will simply co-add the existing profiles.
	If thin is set to False, it will co-add and re-calculate based on the excitation/bg
	temperatures provided.  Currently, molsim can only handle single-excitation temperature
	co-adds with optically thick transmission, as it is not a full non-LTE radiative
	transfer program.  If a resolution is not specified, the highest resolution of the
	input datasets will be used.
	'''

		
	#first figure out what the resolution needs to be if it wasn't set
	if res is None:
		res = min([x.res for x in sims])
		
	#first we find out the limits of the total frequency coverage so we can make an
	#appropriate array to resample onto
	total_freq = np.concatenate([x.spectrum.freq_profile for x in sims])
	total_freq.sort()
	lls,uls = find_limits(total_freq,spacing_tolerance=2,padding=0)
		
	#now make a resampled array
	freq_arr = np.concatenate([np.arange(ll,ul,res) for ll,ul in zip(lls,uls)])	
	int_arr = np.zeros_like(freq_arr)	
	
	#make a spectrum to output
	sum_spectrum = Spectrum(name=name)
	sum_spectrum.freq_profile = freq_arr	

	if thin is True:		
		#loop through the stored simulations, resample them onto freq_arr, add them up
		for x in sims:
			int_arr0 = np.interp(freq_arr,x.spectrum.freq_profile,x.spectrum.int_profile,left=0.,right=0.)
			int_arr += int_arr0				
		sum_spectrum.int_profile = int_arr
		
	if thin is False:
		#if it's not gonna be thin, then we add up all the taus and apply the corrections
		for x in sims:
			int_arr0 = np.interp(freq_arr,x.spectrum.freq_profile,x.spectrum.tau_profile,left=0.,right=0.)
			int_arr += int_arr0
			
		#now we apply the corrections at the specified Tex
		J_T = ((h*freq_arr*10**6/k)*
			  (np.exp(((h*freq_arr*10**6)/
			  (k*Tex))) -1)**-1
			  )
		J_Tbg = ((h*freq_arr*10**6/k)*
			  (np.exp(((h*freq_arr*10**6)/
			  (k*Tbg))) -1)**-1
			  )			  
			
		int_arr = (J_T - J_Tbg)*(1 - np.exp(-int_arr))
		sum_spectrum.int_profile = int_arr

	return sum_spectrum

def velocity_stack(params,name='stack'):
	'''
	Perform a velocity stack.  Requires a params catalog for all the various options.
	Here they are, noted as required, or otherwise have defaults:
	
	selection : 'peaks' or 'lines'. Default: 'lines'
	freq_arr : the array of frequencies. Required
	int_arr : the array of intensities. Required
	freq_sim: the array of simulated frequencies.  Required
	int_sim: the array of simulated intensities. Required
	res_inp : resolution of input data [MHz].  Calculates if not given
	dV : FWHM of lines [km/s]. Required.
	dV_ext : How many dV to integrate over.  Required if 'lines' selected.
	vlsr: vlsr [km/s]. Required.
	vel_width : how many km/s of spectra on either side of a line to stack [km/s].  Required.
	v_res: desired velocity resolution [km/s].  Default: 0.1*dV
	drops: id's of any chunks to exclude.  List.  Default: []
	blank_lines : True or False.  Default: False
	blank_keep_range: range over which not to blank lines.  List [a,b].  Default: 3*dV
	flag_lines: True or False. Default: False
	flag_sigma : number of sigma over which to consider a line an interloper.  Float.  Default: 5.
	'''
	
	#define an obs_chunk class to hold chunks of data to stack
	
	class ObsChunk(object):

		def __init__(self,freq_obs,int_obs,freq_sim,int_sim,peak_int,id):
	
			self.freq_obs = freq_obs #frequency array to be stacked
			self.int_obs = int_obs #intensity array to be stacked
			self.freq_sim = freq_sim #simulated frequency array to be stacked
			self.int_sim = int_sim #simulated intensity array to be stacked
			self.peak_int = peak_int #peak intensity for this chunk
			self.id = id #id of this chunk
			self.flag = False #flagged as not to be used
			self.rms = None #rms of the chunk
			self.cfreq = None #center frequency of the chunk
			self.velocity = None #to hold the velocity array
			
			self.set_rms()
			self.set_cfreq()
			self.set_velocity()
			self.set_sim_velocity()
			
			return
			
		def set_cfreq(self):
			self.cfreq = self.freq_obs[int(round(len(self.freq_obs)/2))]
			return
			
		def set_rms(self):
			self.rms = get_rms(self.int_obs)
			return	
			
		def set_velocity(self):
			velocity = np.zeros_like(self.freq_obs)
			velocity += (self.freq_obs - self.cfreq)*ckm/self.cfreq
			self.velocity = velocity
			return	
			
		def set_sim_velocity(self):
			sim_velocity = np.zeros_like(self.freq_sim)
			sim_velocity += (self.freq_sim - self.cfreq)*ckm/self.cfreq
			self.sim_velocity = sim_velocity
			return				


	#unpacking the dictionary into local variables for ease of use
	options = params.keys()
	freq_arr = params['freq_arr']
	int_arr = params['int_arr']
	freq_sim = params['freq_sim']
	int_sim = params['int_sim']
	res_inp = params['res_inp'] if 'res_inp' in options else _get_res(freq_arr)
	dV = params['dV']
	dV_ext = params['dV_ext'] if 'dV_ext' in options else None
	vlsr = params['vlsr']
	vel_width = params['vel_width']
	v_res = params['v_res'] if 'drops' in options else 0.1*dV
	drops = params['drops'] if 'drops' in options else []
	blank_lines = params['blank_lines'] if 'blank_lines' in options else False
	blank_keep_range = params['blank_keep_range'] if 'blank_keep_range' in options else [-3*dV,3*dV]
	flag_lines = params['flag_lines'] if 'flag_lines' in options else False
	flag_sigma = params['flag_sigma'] if 'flag_sigma' in options else 5.	

	#initialize a spectrum object to hold the stack and name it
	stacked_spectrum = Spectrum(name=name)
	
	#determine the locations to stack and their intensities, either with peaks or lines
	if params['selection'] == 'peaks':
		peak_indices = find_peaks(freq_sim,int_sim,res_inp,dV,is_sim=True)
		peak_freqs = freq_sim[peak_indices]
		peak_ints = int_sim[peak_indices]
		
	if params['selection'] == 'lines':
		peak_indices = find_peaks(freq_sim,int_sim,res_inp,dV*dV_ext,is_sim=True)	
		peak_freqs = freq_sim[peak_indices]
		freq_widths = dV*dV_ext*peak_freqs/ckm
		lls = np.asarray([find_nearest(freq_sim,(x-y/2)) for x,y in zip(peak_freqs,freq_widths)])
		uls = np.asarray([find_nearest(freq_sim,(x+y/2)) for x,y in zip(peak_freqs,freq_widths)])
		peak_ints = np.asarray([np.nansum(int_sim[x:y]) for x,y in zip(lls,uls)])
	
	#split out the data to use, first finding the appropriate indices for the width range we want
	freq_widths = vel_width*peak_freqs/ckm
	lls_obs = np.asarray([find_nearest(freq_arr,x-y) for x,y in zip(peak_freqs,freq_widths)])
	uls_obs = np.asarray([find_nearest(freq_arr,x+y) for x,y in zip(peak_freqs,freq_widths)])
	lls_sim = np.asarray([find_nearest(freq_sim,x-y) for x,y in zip(peak_freqs,freq_widths)])
	uls_sim = np.asarray([find_nearest(freq_sim,x+y) for x,y in zip(peak_freqs,freq_widths)])

	obs_chunks = [ObsChunk(freq_arr[x:y],int_arr[x:y],freq_sim[a:b],int_sim[a:b],peak_int,c) for x,y,a,b,peak_int,c in zip(lls_obs,uls_obs,lls_sim,uls_sim,peak_ints,range(len(uls_sim)))]

	print(vlsr)

	#flagging
	for obs in obs_chunks:
		#already flagged, move on
		if obs.flag is True:
			continue
		#make sure there's data at all.
		if len(obs.freq_obs) == 0:
			obs.flag = True
			continue	
		#drop anything in drops
		if obs.id in drops:
			obs.flag = True
			continue	
		#blank out lines not in the center to be stacked
		if blank_lines is True:
			#Find the indices corresponding to the safe range
			ll_obs = find_nearest(obs.freq_obs,obs.cfreq - blank_keep_range[1]*obs.cfreq/ckm)
			ul_obs = find_nearest(obs.freq_obs,obs.cfreq - blank_keep_range[0]*obs.cfreq/ckm)
			ll_sim = find_nearest(obs.freq_sim,obs.cfreq - blank_keep_range[1]*obs.cfreq/ckm)
			ul_sim = find_nearest(obs.freq_sim,obs.cfreq - blank_keep_range[0]*obs.cfreq/ckm)
			print('{} ({}) {}' .format(obs.cfreq - (blank_keep_range[1])*obs.cfreq/ckm, obs.cfreq, obs.cfreq - (blank_keep_range[0])*obs.cfreq/ckm))
			#store the data in there somewhere temporarily for safekeeping
			obs_safe = np.copy(obs.int_obs[ll_obs:ul_obs])
			sim_safe = np.copy(obs.int_sim[ll_sim:ul_sim])
			#blank the arrays
			obs.int_obs[abs(obs.int_obs) > flag_sigma*obs.rms] = np.nan
			obs_nans_lls,obs_nans_uls = _find_nans(obs.int_obs)
			obs_nans_freqs_lls = obs.int_obs[obs_nans_lls]
			obs_nans_freqs_uls = obs.int_obs[obs_nans_uls]
			sim_nans_lls = [find_nearest(obs.int_sim,x) for x in obs_nans_freqs_lls]
			sim_nans_uls = [find_nearest(obs.int_sim,x) for x in obs_nans_freqs_uls]
			for x,y in zip(sim_nans_lls,sim_nans_uls):
				obs.int_sim[x:y] = np.nan
			obs.int_obs[ll_obs:ul_obs] = np.copy(obs_safe)
			obs.int_sim[ll_sim:ul_sim] = np.copy(sim_safe)	
			#reset the rms, just in case	
			obs.set_rms()	
		#if we're flagging lines in the center, do that now too
		if flag_lines is True:
			if np.nanmax(obs.int_obs) > flag_sigma*obs.rms:
				obs.flag = True
				continue
				
	#setting and applying the weights
	max_int = max(peak_ints)
	for obs in obs_chunks:
		if obs.flag is False:
			obs.weight = obs.peak_int/max_int
			obs.weight /= obs.rms**2
			obs.int_weighted = obs.int_obs * obs.weight
			obs.int_sim_weighted = obs.int_sim * obs.weight		
			
	#Generate a velocity array to interpolate everything onto				
	velocity_avg = np.arange(-vel_width,vel_width,v_res)	
	
	#go through all the chunks and resample them, setting anything that is outside the range we asked for to be nans.
	for obs in obs_chunks:
		if obs.flag is False:
			obs.int_samp = np.interp(velocity_avg,obs.velocity,obs.int_weighted,left=np.nan,right=np.nan)
			obs.int_sim_samp = np.interp(velocity_avg,obs.sim_velocity,obs.int_sim_weighted,left=np.nan,right=np.nan)		
	
	#Now we loop through all the chunks and add them to a list, then convert to an numpy array.  We have to do the same thing w/ RMS values to allow for proper division.
	interped_ints = []
	interped_rms = []
	interped_sim_ints = []
	
	for obs in obs_chunks:
		if obs.flag is False:
			interped_ints.append(obs.int_samp)
			interped_rms.append(obs.rms)
			interped_sim_ints.append(obs.int_sim_samp)
	
	interped_ints = np.asarray(interped_ints)
	interped_rms = np.asarray(interped_rms)
	interped_sim_ints = np.asarray(interped_sim_ints)
	
	#we're going to now need a point by point rms array, so that when we average up and ignore nans, we don't divide by extra values.
	rms_arr = []
	for x in range(len(velocity_avg)):
		rms_sum = 0
		for y in range(len(interped_rms)):
			if np.isnan(interped_ints[y][x]):
				continue
			else:
				rms_sum += interped_rms[y]**2
		rms_arr.append(rms_sum)
	rms_arr	= np.asarray(rms_arr)
	
	#add up the interped intensities, then divide that by the rms_array
	int_avg = np.nansum(interped_ints,axis=0)/rms_arr
	int_sim_avg = np.nansum(interped_sim_ints,axis=0)/rms_arr
	
	#drop some edge channels
	int_avg = int_avg[5:-5]
	int_sim_avg = int_sim_avg[5:-5]
	velocity_avg = velocity_avg[5:-5]
	
	#Get the final rms, and divide out to get to snr.
	rms_tmp = get_rms(int_avg)
	int_avg /= rms_tmp
	int_sim_avg /= rms_tmp
	
	#store everything in the spectrum object and return it
	stacked_spectrum.velocity = velocity_avg
	stacked_spectrum.snr = int_avg
	stacked_spectrum.int_sim = int_sim_avg
						
	return stacked_spectrum,obs_chunks