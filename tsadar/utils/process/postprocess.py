from typing import Dict
from collections import defaultdict

import time, tempfile, mlflow, os, copy

import numpy as np
import scipy.optimize as spopt

from tsadar.utils.plotting import plotters
from tsadar.inverse.loss_function import LossFunction


def recalculate_with_chosen_weights(
    config: Dict, sample_indices, all_data: Dict, loss_fn: LossFunction, calc_sigma: bool, fitted_weights: Dict
):
    """
    Gets parameters and the result of the full forward pass i.e. fits


    Args:
        config: Dict- configuration dictionary built from input deck
        sample_indices:
        all_data: Dict- contains the electron data, ion data, and their respective amplitudes
        loss_fn: Instance of the LossFunction class
        fitted_weights: Dict- best values of the parameters returned by the minimizer

    Returns:

    """

    losses = np.zeros_like(sample_indices, dtype=np.float64)
    sample_indices.sort()
    batch_indices = np.reshape(sample_indices, (-1, config["optimizer"]["batch_size"]))

    # turn list of dictionaries into dictionary of lists
    all_params = {k: defaultdict(list) for k in config["parameters"].keys()}

    for _fw in fitted_weights:
        unnormed_params = _fw.get_unnormed_params()
        for k in all_params.keys():
            for k2 in unnormed_params[k].keys():
                all_params[k][k2].append(unnormed_params[k][k2])

    # concatenate all the lists in the dictionary
    num_params = 0
    for k in all_params.keys():
        for k2 in all_params[k].keys():
            all_params[k][k2] = np.concatenate(all_params[k][k2])
            num_params += len(all_params[k][k2])

    fits = {}
    sqdevs = {}
    fits["ion"] = np.zeros(all_data["i_data"].shape)
    sqdevs["ion"] = np.zeros(all_data["i_data"].shape)
    fits["ele"] = np.zeros(all_data["e_data"].shape)
    sqdevs["ele"] = np.zeros(all_data["e_data"].shape)

    if config["other"]["extraoptions"]["load_ion_spec"]:
        sigmas = np.zeros((all_data["i_data"].shape[0], num_params))

    if config["other"]["extraoptions"]["load_ele_spec"]:
        sigmas = np.zeros((all_data["e_data"].shape[0], num_params))

    if config["other"]["extraoptions"]["spectype"] == "angular_full":
        batch = {
            "e_data": all_data["e_data"][config["data"]["lineouts"]["start"] : config["data"]["lineouts"]["end"], :],
            "e_amps": all_data["e_amps"][config["data"]["lineouts"]["start"] : config["data"]["lineouts"]["end"], :],
            "i_data": all_data["i_data"],
            "i_amps": all_data["i_amps"],
            "noise_e": all_data["noiseE"][config["data"]["lineouts"]["start"] : config["data"]["lineouts"]["end"], :],
            "noise_i": all_data["noiseI"][config["data"]["lineouts"]["start"] : config["data"]["lineouts"]["end"], :],
        }
        losses, sqds, used_points, [ThryE, _, params] = loss_fn.array_loss(fitted_weights, batch)
        fits["ele"] = ThryE
        sqdevs["ele"] = sqds["ele"]

        for species in all_params.keys():
            for k in all_params[species].keys():
                if k != "fe":
                    # all_params[k] = np.concatenate([all_params[k], params[k].reshape(-1)])
                    all_params[species][k] = params[species][k].reshape(-1)
                else:
                    all_params[species][k] = params[species][k]

        if calc_sigma:
            # this line may need to be omited since the weights may be transformed by line 77
            active_params = loss_fn.spec_calc.get_plasma_parameters(fitted_weights, return_static_params=False)
            hess = loss_fn.h_loss_wrt_params(active_params, batch)
            sigmas = get_sigmas(hess, config["optimizer"]["batch_size"])
            print(f"Number of 0s in sigma: {len(np.where(sigmas==0)[0])}")

    else:
        for i_batch, inds in enumerate(batch_indices):
            batch = {
                "e_data": all_data["e_data"][inds],
                "e_amps": all_data["e_amps"][inds],
                "i_data": all_data["i_data"][inds],
                "i_amps": all_data["i_amps"][inds],
                "noise_e": all_data["noiseE"][inds],
                "noise_i": all_data["noiseI"][inds],
            }

            loss, sqds, used_points, ThryE, ThryI, params = loss_fn.array_loss(fitted_weights[i_batch], batch)

            if calc_sigma:
                hess = loss_fn.h_loss_wrt_params(fitted_weights[i_batch], batch)
                try:
                    hess = loss_fn.h_loss_wrt_params(fitted_weights[i_batch], batch)
                except:
                    print("Error calculating Hessian, no hessian based uncertainties have been calculated")
                    calc_sigma = False

            losses[inds] = loss
            sqdevs["ele"][inds] = sqds["ele"]
            sqdevs["ion"][inds] = sqds["ion"]
            if calc_sigma:
                sigmas[inds] = get_sigmas(hess, config["optimizer"]["batch_size"])
                # print(f"Number of 0s in sigma: {len(np.where(sigmas==0)[0])}") number of negatives?

            fits["ele"][inds] = ThryE
            fits["ion"][inds] = ThryI

    return losses, sqdevs, used_points, fits, sigmas, all_params


def get_sigmas(hess: Dict, batch_size: int) -> Dict:
    """
    Calculates the variance using the hessian with respect to the parameters and then using the hessian values
    as the inverse of the covariance matrix and then inverting that. Negatives in the inverse hessian normally indicate
    non-optimal points, to represent this in the final result the uncertainty of those values are reported as negative.


    Args:
        hess: Hessian dictionary, the field for each fitted parameter has subfields corresponding to each of the other
            fitted parameters. Within each nested subfield is a batch_size x batch_size array with the hessian values
            for that parameter combination and that batch. The cross terms of this array are zero since separate
            lineouts within a batch do not affect each other, they are therefore discarded
        batch_size: int- number of lineouts in a batch

    Returns:
        sigmas: batch_size x number_of_parameters array with the uncertainty values for each parameter
    """
    sizes = {
        key + species: hess[species][key][species][key].shape[1]
        for species in hess.keys()
        for key in hess[species].keys()
    }
    # sizes = {key: hess[key][key].shape[1] for key in keys}
    actual_num_params = sum([v for k, v in sizes.items()])
    sigmas = np.zeros((batch_size, actual_num_params))

    for i in range(batch_size):
        temp = np.zeros((actual_num_params, actual_num_params))
        k1 = 0
        for species1 in hess.keys():
            for key1 in hess[species1].keys():
                k2 = 0
                for species2 in hess.keys():
                    for key2 in hess[species2].keys():
                        temp[k1, k2] = np.squeeze(hess[species1][key1][species2][key2])[i, i]
                        k2 += 1
                k1 += 1

        # xc = 0
        # for k1, param in enumerate(keys):
        #     yc = 0
        #     for k2, param2 in enumerate(keys):
        #         if i > 0:
        #             temp[k1, k2] = np.squeeze(hess[param][param2])[i, i]
        #         else:
        #             temp[xc : xc + sizes[param], yc : yc + sizes[param2]] = hess[param][param2][0, :, 0, :]
        #
        #         yc += sizes[param2]
        #     xc += sizes[param]

        # print(temp)
        inv = np.linalg.inv(temp)
        # print(inv)

        sigmas[i, :] = np.sign(np.diag(inv)) * np.sqrt(np.abs(np.diag(inv)))
        # for k1, param in enumerate(keys):
        #     sigmas[i, xc : xc + sizes[param]] = np.diag(
        #         np.sign(inv[xc : xc + sizes[param], xc : xc + sizes[param]])
        #         * np.sqrt(np.abs(inv[xc : xc + sizes[param], xc : xc + sizes[param]]))
        #     )
        # print(sigmas[i, k1])
        # change sigmas into a dictionary?

    return sigmas


def postprocess(config, sample_indices, all_data: Dict, all_axes: Dict, loss_fn, sa, fitted_weights):
    t1 = time.time()

    for species in config["parameters"].keys():
        if "electron" == species:
            elec_species = species

    if config["other"]["extraoptions"]["spectype"] != "angular_full" and config["other"]["refit"]:
        refit_bad_fits(config, sample_indices, all_data, loss_fn, sa, fitted_weights)

    mlflow.log_metrics({"refitting time": round(time.time() - t1, 2)})

    with tempfile.TemporaryDirectory() as td:
        _ = [os.makedirs(os.path.join(td, dirname), exist_ok=True) for dirname in ["plots", "binary", "csv"]]
        if config["other"]["extraoptions"]["spectype"] == "angular_full":
            t1 = process_angular_data(
                config, sample_indices, all_data, all_axes, loss_fn, fitted_weights, t1, elec_species, td
            )

        else:
            t1, final_params = process_data(config, sample_indices, all_data, all_axes, loss_fn, fitted_weights, t1, td)

        mlflow.log_artifacts(td)
    mlflow.log_metrics({"plotting time": round(time.time() - t1, 2)})

    mlflow.set_tag("status", "done plotting")

    return final_params


def refit_bad_fits(config, batch_indices, all_data, loss_fn, sa, fitted_weights):
    losses_init, sqdevs, used_points, fits, sigmas, all_params = recalculate_with_chosen_weights(
        config, batch_indices, all_data, loss_fn, False, fitted_weights
    )

    # refit bad fits
    red_losses_init = losses_init / (1.1 * (used_points - len(all_params)))
    true_batch_size = config["optimizer"]["batch_size"]
    # config["optimizer"]["batch_size"] = 1
    mlflow.log_metrics({"number of fits": len(batch_indices.flatten())})
    mlflow.log_metrics({"number of refits": int(np.sum(red_losses_init > config["other"]["refit_thresh"]))})

    for i in batch_indices.flatten()[red_losses_init > config["other"]["refit_thresh"]]:
        if i == 0:
            continue

        batch = {
            "e_data": np.reshape(all_data["e_data"][i], (1, -1)),
            "e_amps": np.reshape(all_data["e_amps"][i], (1, -1)),
            "i_data": np.reshape(all_data["i_data"][i], (1, -1)),
            "i_amps": np.reshape(all_data["i_amps"][i], (1, -1)),
            "noise_e": np.reshape(all_data["noiseE"][i], (1, -1)),
            "noise_i": np.reshape(all_data["noiseI"][i], (1, -1)),
        }

        # previous_weights = {}
        temp_cfg = copy.copy(config)
        temp_cfg["optimizer"]["batch_size"] = 1
        for species in fitted_weights[(i - 1) // true_batch_size].keys():
            for key in fitted_weights[(i - 1) // true_batch_size][species].keys():
                if config["parameters"][species][key]["active"]:
                    temp_cfg["parameters"][species][key]["val"] = float(
                        fitted_weights[(i - 1) // true_batch_size][species][key][(i - 1) % true_batch_size]
                    )

        loss_fn_refit = LossFunction(temp_cfg, sa, batch)

        # loss_fn_refit.flattened_weights, loss_fn_refit.unravel_pytree = ravel_pytree(previous_weights)

        res = spopt.minimize(
            loss_fn_refit.vg_loss if config["optimizer"]["grad_method"] == "AD" else loss_fn_refit.loss,
            np.copy(loss_fn_refit.flattened_weights),
            args=batch,
            method=config["optimizer"]["method"],
            jac=True if config["optimizer"]["grad_method"] == "AD" else False,
            bounds=loss_fn_refit.bounds,
            options={"disp": True, "maxiter": config["optimizer"]["num_epochs"]},
        )
        cur_result = loss_fn_refit.unravel_pytree(res["x"])

        for species in cur_result.keys():
            for key in cur_result[species].keys():
                fitted_weights[i // true_batch_size][species][key] = (
                    fitted_weights[i // true_batch_size][species][key]
                    .at[i % true_batch_size]
                    .set(cur_result[species][key][0])
                )
                # fitted_weights[i // true_batch_size][species][key][i % true_batch_size] = cur_result[species][key]

            # for key in fitted_weights[i // true_batch_size].keys():
            #     cur_value = cur_result[key][0, 0]
            #     new_vals = fitted_weights[i // true_batch_size][key]
            #     new_vals = new_vals.at[tuple([i % true_batch_size, 0])].set(cur_value)
            #     fitted_weights[i // true_batch_size][key] = new_vals

    config["optimizer"]["batch_size"] = true_batch_size


def process_data(config, sample_indices, all_data, all_axes, loss_fn, fitted_weights, t1, td):
    losses, sqdevs, used_points, fits, sigmas, all_params = recalculate_with_chosen_weights(
        config, sample_indices, all_data, loss_fn, config["other"]["calc_sigmas"], fitted_weights
    )
    if "losses_init" not in locals():
        losses_init = losses
    mlflow.log_metrics({"postprocessing time": round(time.time() - t1, 2)})
    mlflow.set_tag("status", "plotting")
    t1 = time.time()

    final_params = plotters.get_final_params(config, all_params, all_axes, td)

    red_losses = plotters.plot_loss_hist(config, losses_init, losses, all_params, used_points, td)
    savedata = plotters.plot_ts_data(config, fits, all_data, all_axes, td)
    plotters.model_v_actual(config, all_data, all_axes, fits, losses, red_losses, sqdevs, td)
    sigma_ds = plotters.save_sigmas_params(config, all_params, sigmas, all_axes, td)
    plotters.plot_final_params(config, all_params, sigma_ds, td)
    return t1, final_params


def process_angular_data(config, batch_indices, all_data, all_axes, loss_fn, fitted_weights, t1, elec_species, td):
    best_weights_val = {}
    best_weights_std = {}
    if config["optimizer"]["num_mins"] > 1:
        for k, v in fitted_weights.items():
            best_weights_val[k] = np.average(v, axis=0)  # [0, :]
            best_weights_std[k] = np.std(v, axis=0)  # [0, :]
    else:
        best_weights_val = fitted_weights

    losses, sqdevs, used_points, fits, sigmas, all_params = recalculate_with_chosen_weights(
        config, batch_indices, all_data, loss_fn, config["other"]["calc_sigmas"], best_weights_val
    )

    mlflow.log_metrics({"postprocessing time": round(time.time() - t1, 2)})
    mlflow.set_tag("status", "plotting")
    t1 = time.time()

    final_params = plotters.get_final_params(config, all_params, all_axes, td)
    if config["other"]["calc_sigmas"]:
        sigma_fe = plotters.save_sigmas_fe(final_params, best_weights_std, sigmas, td)
    else:
        sigma_fe = np.zeros_like(final_params["fe"])
    savedata = plotters.plot_data_angular(config, fits, all_data, all_axes, td)
    plotters.plot_ang_lineouts(used_points, sqdevs, losses, all_params, all_axes, savedata, td)
    # plotters.plot_dist(config, elec_species, final_params, sigma_fe, td)
    return t1
