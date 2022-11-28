import pretty_errors
from utils import none_checker, ConfigParser, download_online_file, load_local_csv_as_darts_timeseries, truth_checker, load_yaml_as_dict, load_model, load_scaler, multiple_dfs_to_ts_file
from preprocessing import scale_covariates, split_dataset

from functools import reduce
from darts.metrics import mape as mape_darts
from darts.metrics import mase as mase_darts
from darts.metrics import mae as mae_darts
from darts.metrics import rmse as rmse_darts
from darts.metrics import smape as smape_darts
from darts.models import (
    NaiveSeasonal,
)
# the following are used through eval(darts_model + 'Model')
from darts.models import RNNModel, BlockRNNModel, NBEATSModel, TFTModel, NaiveDrift, NaiveSeasonal, TCNModel
# from darts.models.forecasting.auto_arima import AutoARIMA
from darts.models.forecasting.gradient_boosted_model import LightGBMModel
from darts.models.forecasting.random_forest import RandomForest
from darts.utils.likelihood_models import ContinuousBernoulliLikelihood, GaussianLikelihood, DirichletLikelihood, ExponentialLikelihood, GammaLikelihood, GeometricLikelihood

import yaml
import mlflow
import click
import os
import torch
import logging
import pickle
import tempfile
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
import shutil
import optuna
import pandas as pd
# Inference requirements to be stored with the darts flavor !!
from sys import version_info
import torch, cloudpickle, darts
import matplotlib.pyplot as plt
import pprint
from sklearn.metrics import mean_absolute_percentage_error as mape
from sklearn.metrics import mean_squared_error as mse

PYTHON_VERSION = "{major}.{minor}.{micro}".format(major=version_info.major,
                                                  minor=version_info.minor,
                                                  micro=version_info.micro)
mlflow_serve_conda_env = {
    'channels': ['defaults'],
    'dependencies': [
        'python={}'.format(PYTHON_VERSION),
        'pip',
        {
            'pip': [
                'cloudpickle=={}'.format(cloudpickle.__version__),
                'darts=={}'.format(darts.__version__),
                'pretty_errors=={}'.format(pretty_errors.__version__),
                'torch=={}'.format(torch.__version__),
                'mlflow=={}'.format(mlflow.__version__)
            ],
        },
    ],
    'name': 'darts_infer_pl_env'
}

# get environment variables
from dotenv import load_dotenv
load_dotenv()
# explicitly set MLFLOW_TRACKING_URI as it cannot be set through load_dotenv
# os.environ["MLFLOW_TRACKING_URI"] = ConfigParser().mlflow_tracking_uri
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI")

# stop training when validation loss does not decrease more than 0.05 (`min_delta`) over
# a period of 5 epochs (`patience`)
def log_optuna(study, opt_tmpdir, hyperparams_entrypoint, mlrun, log_model=False, curr_mape=0, model=None, darts_model=None, scale=False, scalers_dir=None, features_dir=None, past_covariates=None, future_covariates=None):
    
    if log_model and (len(study.trials_dataframe()[study.trials_dataframe()["state"] == "COMPLETE"]) < 1 or study.best_trial.values[0] >= curr_mape):
        if darts_model in ['NHiTS', 'NBEATS', 'RNN', 'BlockRNN', 'TFT', 'TCN']:
            logs_path = f"./darts_logs/{mlrun.info.run_id}"
            model_type = "pl"
        elif darts_model in ['LightGBM', 'RandomForest']:
            print('\nStoring the model as pkl to MLflow...')
            logging.info('\nStoring the model as pkl to MLflow...')
            forest_dir = tempfile.mkdtemp()

            pickle.dump(model, open(
                f"{forest_dir}/_model.pkl", "wb"))

            logs_path = forest_dir
            model_type = "pkl"

        if scale:
            source_dir = scalers_dir
            target_dir = logs_path
            file_names = os.listdir(source_dir)
            for file_name in file_names:
                shutil.move(os.path.join(source_dir, file_name),
                target_dir)
            
        ## Create and move model info in logs path
        model_info_dict = {
            "darts_forecasting_model":  model.__class__.__name__,
            "run_id": mlrun.info.run_id
        }
        with open('model_info.yml', mode='w') as outfile:
            yaml.dump(
                model_info_dict,
                outfile,
                default_flow_style=False)

        
        shutil.move('model_info.yml', logs_path)

        ## Rename logs path to get rid of run name
        if model_type == 'pkl':
            logs_path_new = logs_path.replace(
            forest_dir.split('/')[-1], mlrun.info.run_id)
            os.rename(logs_path, logs_path_new)
        elif model_type == 'pl':
            logs_path_new = logs_path
        
        mlflow_model_root_dir = "pyfunc_model"
            
        ## Log MLflow model and code
        mlflow.pyfunc.log_model(mlflow_model_root_dir,
                            loader_module="darts_flavor",
                            data_path=logs_path_new,
                            code_path=['utils.py', 'inference.py', 'darts_flavor.py'],
                            conda_env=mlflow_serve_conda_env)
            
        shutil.rmtree(logs_path_new)

        print("\nArtifacts are being uploaded to MLflow...")
        logging.info("\nArtifacts are being uploaded to MLflow...")
        mlflow.log_artifacts(features_dir, "features")

        if scale:
            # mlflow.log_artifacts(scalers_dir, f"{mlflow_model_path}/scalers")
            mlflow.set_tag(
                'scaler_uri',
                f'{mlrun.info.artifact_uri}/{mlflow_model_root_dir}/data/{mlrun.info.run_id}/scaler_series.pkl')
        else:
            mlflow.set_tag('scaler_uri', 'mlflow_artifact_uri')



        mlflow.set_tag("run_id", mlrun.info.run_id)
        mlflow.set_tag("stage", "optuna_search")
        mlflow.set_tag("model_type", model_type)

        mlflow.set_tag("darts_forecasting_model",
            model.__class__.__name__)
        # model_uri
        mlflow.set_tag('model_uri', mlflow.get_artifact_uri(
            f"{mlflow_model_root_dir}/data/{mlrun.info.run_id}"))
        # inference_model_uri
        mlflow.set_tag('pyfunc_model_folder', mlflow.get_artifact_uri(
            f"{mlflow_model_root_dir}"))

        mlflow.set_tag('series_uri',
            f'{mlrun.info.artifact_uri}/features/series.csv')

        if future_covariates is not None:
            mlflow.set_tag(
                'future_covariates_uri',
                f'{mlrun.info.artifact_uri}/features/future_covariates_transformed.csv')
        else:
            mlflow.set_tag(
                'future_covariates_uri',
                'mlflow_artifact_uri')

        if past_covariates is not None:
            mlflow.set_tag(
                'past_covariates_uri',
                f'{mlrun.info.artifact_uri}/features/past_covariates_transformed.csv')
        else:
            mlflow.set_tag('past_covariates_uri',
                'mlflow_artifact_uri')

        print("\nArtifacts uploaded.")
        logging.info("\nArtifacts uploaded.")
    else:
        ######################
        # Log hyperparameters
        mlflow.log_params(study.best_params)

        # Log log_metrics
        mlflow.log_metrics(study.best_trial.user_attrs)




    if len(study.trials_dataframe()[study.trials_dataframe()["state"] == "COMPLETE"]) <= 1: return

    plt.close()

    fig = optuna.visualization.plot_optimization_history(study)
    fig.write_html(f"{opt_tmpdir}/plot_optimization_history.html")
    plt.close()

    fig = optuna.visualization.plot_param_importances(study)
    fig.write_html(f"{opt_tmpdir}/plot_param_importances.html")
    plt.close()

    fig = optuna.visualization.plot_slice(study)
    fig.write_html(f"{opt_tmpdir}/plot_slice.html")
    plt.close()

    study.trials_dataframe().to_csv(f"{opt_tmpdir}/{hyperparams_entrypoint}.csv")

    print("\nUploading optuna plots to MLflow server...")
    logging.info("\nUploading optuna plots to MLflow server...")

    mlflow.log_artifacts(opt_tmpdir, "optuna_results")

def append(x, y):
    return x.append(y)

def objective(series_uri, future_covs_uri, year_range, resolution, time_covs,
             darts_model, hyperparams_entrypoint, cut_date_val, test_end_date, cut_date_test, device,
             forecast_horizon, stride, retrain, scale, scale_covs, multiple,
             eval_country, mlrun, trial, study, opt_tmpdir):

                hyperparameters = ConfigParser('config_opt.yml').read_hyperparameters(hyperparams_entrypoint)
                training_dict = {}
                for param, value in hyperparameters.items():
                    if type(value) == list and value and value[0] == "range":
                        if type(value[1]) == int:
                            training_dict[param] = trial.suggest_int(param, value[1], value[2], value[3])
                        else:
                            training_dict[param] = trial.suggest_float(param, value[1], value[2], value[3])
                    elif type(value) == list and value and value[0] == "list":
                        training_dict[param] = trial.suggest_categorical(param, value[1:])
                    else:
                        training_dict[param] = value
                if 'scale' in training_dict:
                     scale = training_dict['scale']
                     del training_dict['scale']
                     scale = "False"

                model, scaler, train_future_covariates, train_past_covariates, features_dir, scalers_dir = train(
                      series_uri=series_uri,
                      future_covs_uri=future_covs_uri,
                      past_covs_uri=None, # fix that in case REAL Temperatures come -> etl_temp_covs_uri. For forecasts, integrate them into future covariates!!
                      darts_model=darts_model,
                      hyperparams_entrypoint=hyperparams_entrypoint,
                      cut_date_val=cut_date_val,
                      cut_date_test=cut_date_test,
                      test_end_date=test_end_date,
                      device=device,
                      scale=scale,
                      scale_covs=scale_covs,
                      multiple=multiple,
                      training_dict=training_dict,
                      mlrun=mlrun,
                      )
                try:
                    trial.set_user_attr("epochs_trained", model.epochs_trained)
                except:
                    pass
                metrics = validate(
                    series_uri=series_uri,
                    future_covariates=train_future_covariates,
                    past_covariates=train_past_covariates,
                    scaler=scaler,
                    cut_date_test=cut_date_test,
                    test_end_date=test_end_date,#check that again
                    model=model,
                    forecast_horizon=forecast_horizon,
                    stride=stride,
                    retrain=retrain,
                    multiple=multiple,
                    eval_country=eval_country,
                    cut_date_val=cut_date_val,
                    mlrun=mlrun,
                    )
                trial.set_user_attr("mape", float(metrics["mape"]))
                trial.set_user_attr("smape", float(metrics["smape"]))
                trial.set_user_attr("mase", float(metrics["mase"]))
                trial.set_user_attr("mae", float(metrics["mae"]))
                trial.set_user_attr("rmse", float(metrics["rmse"]))

                log_optuna(study, opt_tmpdir, hyperparams_entrypoint, mlrun, log_model=True, curr_mape=float(metrics["mape"]), model=model, darts_model=darts_model, scale=scale, scalers_dir=scalers_dir, features_dir=features_dir, past_covariates=train_past_covariates, future_covariates=train_future_covariates)

                return metrics["mape"]

def train(series_uri, future_covs_uri, past_covs_uri, darts_model,
          hyperparams_entrypoint, cut_date_val, cut_date_test,
          test_end_date, device, scale, scale_covs, multiple,
          training_dict, mlrun):


    # Argument preprocessing

    ## test_end_date
    my_stopper = EarlyStopping(
        monitor="val_loss",
        patience=10,
        min_delta=1e-6,
        mode='min',
    )

    test_end_date = none_checker(test_end_date)

    ## scale or not
    scale = truth_checker(scale)
    scale_covs = truth_checker(scale_covs)

    multiple = truth_checker(multiple)

    ## hyperparameters
    hyperparameters = training_dict

    ## device
    #print("param", hyperparameters)
    if device == 'gpu' and torch.cuda.is_available():
        device = 'gpu'
        print("\nGPU is available")
    else:
        device = 'cpu'
        print("\nGPU is available")
    ## series and covariates uri and csv
    series_uri = none_checker(series_uri)
    future_covs_uri = none_checker(future_covs_uri)
    past_covs_uri = none_checker(past_covs_uri)

    # redirect to local location of downloaded remote file
    if series_uri is not None:
        download_file_path = download_online_file(series_uri, dst_filename="load.csv")
        series_csv = download_file_path.replace('/', os.path.sep).replace("'", "")
    else:
        series_csv = None

    if  future_covs_uri is not None:
        download_file_path = download_online_file(future_covs_uri, dst_filename="future.csv")
        future_covs_csv = download_file_path.replace('/', os.path.sep).replace("'", "")
    else:
        future_covs_csv = None

    if  past_covs_uri is not None:
        download_file_path = download_online_file(past_covs_uri, dst_filename="past.csv")
        past_covs_csv = download_file_path.replace('/', os.path.sep).replace("'", "")
    else:
        past_covs_csv = None

    ## model
    # TODO: Take care of future covariates (RNN, ...) / past covariates (BlockRNN, NBEATS, ...)
    if darts_model in ["NBEATS", "BlockRNN", "TCN"]:
        """They do not accept future covariates as they predict blocks all together.
        They won't use initial forecasted values to predict the rest of the block
        So they won't need to additionally feed future covariates during the recurrent process.
        """
        past_covs_csv = future_covs_csv
        future_covs_csv = None
        # TODO: when actual weather comes extend it, now the stage only accepts future covariates as argument.

    elif darts_model in ["RNN"]:
        """Does not accept past covariates as it needs to know future ones to provide chain forecasts
        its input needs to remain in the same feature space while recurring and with no future covariates
        this is not possible. The existence of past_covs is not permitted for the same reason. The
        feature space will change during inference. If for example I have current temperature and during
        the forecast chain I only have time covariates, as I won't know the real temp then a constant \
        architecture like LSTM cannot handle this"""
        past_covs_csv = None
        # TODO: when actual weather comes extend it, now the stage only accepts future covariates as argument.
    #elif: extend for other models!! (time_covariates are always future covariates, but some models can't handle them as so)

    future_covariates = none_checker(future_covs_csv)
    past_covariates = none_checker(past_covs_csv)


    ######################
    # Load series and covariates datasets
    time_col = "Date"
    series, country_l, country_code_l = load_local_csv_as_darts_timeseries(
                local_path=series_csv,
                name='series',
                time_col=time_col,
                last_date=test_end_date,
                multiple=multiple)
    if future_covariates is not None:
        future_covariates, _, _ = load_local_csv_as_darts_timeseries(
                local_path=future_covs_csv,
                name='future covariates',
                time_col=time_col,
                last_date=test_end_date)
    if past_covariates is not None:
        past_covariates, _, _ = load_local_csv_as_darts_timeseries(
                local_path=past_covs_csv,
                name='past covariates',
                time_col=time_col,
                last_date=test_end_date)

    if scale:
        scalers_dir = tempfile.mkdtemp()
    features_dir = tempfile.mkdtemp()

    ######################
    # Train / Test split
    print(
        f"\nTrain / Test split: Validation set starts: {cut_date_val} - Test set starts: {cut_date_test} - Test set end: {test_end_date}")
    logging.info(
        f"\nTrain / Test split: Validation set starts: {cut_date_val} - Test set starts: {cut_date_test} - Test set end: {test_end_date}")

    ## series
    series_split = split_dataset(
        series,
        val_start_date_str=cut_date_val,
        test_start_date_str=cut_date_test,
        test_end_date=test_end_date,
        store_dir=features_dir,        
        name='series',
        multiple=multiple,
        country_l=country_l,
        country_code_l=country_code_l,
            )
        ## future covariates
    future_covariates_split = split_dataset(
        future_covariates,
        val_start_date_str=cut_date_val,
        test_start_date_str=cut_date_test,
        test_end_date=test_end_date,
        # store_dir=features_dir,
        name='future_covariates',
        )
    ## past covariates
    past_covariates_split = split_dataset(
        past_covariates,
        val_start_date_str=cut_date_val,
        test_start_date_str=cut_date_test,
        test_end_date=test_end_date,
        # store_dir=features_dir,
        name='past_covariates',
        )

    #################
    # Scaling
    print("\nScaling...")
    logging.info("\nScaling...")

    ## scale series
    series_transformed = scale_covariates(
        series_split,
        store_dir=features_dir,
        filename_suffix="series_transformed.csv",
        scale=scale,
        multiple=multiple,
        country_l=country_l,
        country_code_l=country_code_l,
        )
    
    if scale:
        pickle.dump(series_transformed["transformer"], open(f"{scalers_dir}/scaler_series.pkl", "wb"))

    ## scale future covariates
    future_covariates_transformed = scale_covariates(
        future_covariates_split,
        scale=scale_covs,
        store_dir=features_dir,
        filename_suffix="future_covariates_transformed.csv",
        )
    ## scale past covariates
    past_covariates_transformed = scale_covariates(
        past_covariates_split,
        scale=scale_covs,
        store_dir=features_dir,
        filename_suffix="past_covariates_transformed.csv",
        )
    ######################
    # Model training
    print("\nTraining model...")
    logging.info("\nTraining model...")
    pl_trainer_kwargs = {"callbacks": [my_stopper],
                         "accelerator": 'auto',
                        #  "gpus": 1,
                        #  "auto_select_gpus": True,
                         "log_every_n_steps": 10}
    ## choose architecture
    if darts_model in ['NBEATS', 'RNN', 'BlockRNN', 'TFT', 'TCN']:
        hparams_to_log = hyperparameters
        if 'learning_rate' in hyperparameters:
            hyperparameters['optimizer_kwargs'] = {'lr': hyperparameters['learning_rate']}
            del hyperparameters['learning_rate']

        if 'likelihood' in hyperparameters:
            hyperparameters['likelihood'] = eval(hyperparameters['likelihood']+"Likelihood"+"()")
        print(hyperparameters)
        model = eval(darts_model + 'Model')(
            force_reset=True,
            save_checkpoints=True,
            log_tensorboard=False,
            model_name=mlrun.info.run_id,
            pl_trainer_kwargs=pl_trainer_kwargs,
            **hyperparameters
        )
        ## fit model
        # try:
        # print(series_transformed['train'])
        # print(series_transformed['val'])
        print("SERIES", series_transformed['train'])
        model.fit(series_transformed['train'],
            future_covariates=future_covariates_transformed['train'],
            past_covariates=past_covariates_transformed['train'],
            val_series=series_transformed['val'],
            val_future_covariates=future_covariates_transformed['val'],
            val_past_covariates=past_covariates_transformed['val'])

        # LightGBM and RandomForest
    elif darts_model in ['LightGBM', 'RandomForest']:

        if "lags_future_covariates" in hyperparameters:
            if truth_checker(str(hyperparameters["future_covs_as_tuple"])):
                hyperparameters["lags_future_covariates"] = tuple(
                    hyperparameters["lags_future_covariates"])
            hyperparameters.pop("future_covs_as_tuple")

        if future_covariates is None:
            hyperparameters["lags_future_covariates"] = None
        if past_covariates is None:
            hyperparameters["lags_past_covariates"] = None

        hparams_to_log = hyperparameters

        if darts_model == 'RandomForest':
            model = RandomForest(**hyperparameters)
        elif darts_model == 'LightGBM':
            model = LightGBMModel(**hyperparameters)

        print(f'\nTraining {darts_model}...')
        logging.info(f'\nTraining {darts_model}...')

        model.fit(
            series=series_transformed['train'],
            # val_series=series_transformed['val'],
            future_covariates=future_covariates_transformed['train'],
            past_covariates=past_covariates_transformed['train'],
            # val_future_covariates=future_covariates_transformed['val'],
            # val_past_covariates=past_covariates_transformed['val']
            )
    if scale:
        scaler = series_transformed["transformer"]
    else:
        scaler = None

    if future_covariates is not None:
        return_future_covariates = future_covariates_transformed['all']
    else:
        return_future_covariates = None

    if past_covariates is not None:
        return_past_covariates = past_covariates_transformed['all']
    else:
        return_past_covariates = None
    return model, scaler, return_future_covariates, return_past_covariates, features_dir, scalers_dir

def backtester(model,
               series_transformed,
               test_start_date,
               forecast_horizon,
               stride=None,
               series=None,
               transformer_ts=None,
               retrain=False,
               future_covariates=None,
               past_covariates=None,
               path_to_save_backtest=None):
    """ Does the same job with advanced forecast but much more quickly using the darts
    bult-in historical_forecasts method. Use this for evaluation. The other only
    provides pure inference. Provide a unified timeseries test set point based
    on test_start_date. series_transformed does not need to be adjacent to
    training series. if transformer_ts=None then no inverse transform is applied
    to the model predictions.
    Parameters
    ----------
    Returns
    ----------
    """
    # produce the fewest forecasts possible.
    if stride is None:
        stride = forecast_horizon
    test_start_date = pd.Timestamp(test_start_date)

    # produce list of forecasts
    #print("backtesting starting at", test_start_date, "series:", series_transformed)
    backtest_series_transformed = model.historical_forecasts(series_transformed,
                                                             future_covariates=future_covariates,
                                                             past_covariates=past_covariates,
                                                             start=test_start_date,
                                                             forecast_horizon=forecast_horizon,
                                                             stride=stride,
                                                             retrain=retrain,
                                                             last_points_only=False,
                                                             verbose=False)

    # flatten lists of forecasts due to last_points_only=False
    if isinstance(backtest_series_transformed, list):
        backtest_series_transformed = reduce(
            append, backtest_series_transformed)

    # inverse scaling
    if transformer_ts is not None and series is not None:
        backtest_series = transformer_ts.inverse_transform(
            backtest_series_transformed)
    else:
        series = series_transformed
        backtest_series = backtest_series_transformed
        print("\nWarning: Scaler not provided. Ensure model provides normal scale predictions")
        logging.info(
            "\n Warning: Scaler not provided. Ensure model provides normal scale predictions")

    # Metrix
    test_series = series.drop_before(pd.Timestamp(test_start_date))
    print("SERIES",test_series)
    metrics = {
        "mape": mape_darts(
            test_series,
            backtest_series),
        "smape": smape_darts(
            test_series,
            backtest_series),
        "mase": mase_darts(
            series.drop_before(pd.Timestamp(test_start_date)),
            backtest_series,
            insample=series.drop_after(pd.Timestamp(test_start_date))),
        "mae": mae_darts(
            series.drop_before(pd.Timestamp(test_start_date)),
            backtest_series),
        "rmse": rmse_darts(
            series.drop_before(pd.Timestamp(test_start_date)),
            backtest_series)
    }
    for key, value in metrics.items():
        print(key, ': ', value)

    return {"metrics": metrics, "backtest_series": backtest_series}


def validate(series_uri, future_covariates, past_covariates, scaler, cut_date_test, test_end_date,
             model, forecast_horizon, stride, retrain, multiple, eval_country, cut_date_val, mlrun, mode='remote'):
    # TODO: modify functions to support models with likelihood != None
    # TODO: Validate evaluation step for all models. It is mainly tailored for the RNNModel for now.

    # Argument processing
    stride = none_checker(stride)
    forecast_horizon = int(forecast_horizon)
    stride = int(forecast_horizon) if stride is None else int(stride)
    retrain = truth_checker(retrain)
    multiple = truth_checker(multiple)

    future_covariates = none_checker(future_covariates)
    past_covariates = none_checker(past_covariates)

    # Load model / datasets / scalers from Mlflow server

    ## load series from MLflow
    series_path = download_online_file(
        series_uri, "series.csv") if mode == 'remote' else series_uri
    series, country_l, country_code_l = load_local_csv_as_darts_timeseries(
        local_path=series_path,
        last_date=test_end_date,
        multiple=multiple)

    if scaler is not None:
        if not multiple:
            series_transformed = scaler.transform(series)
        else:
            series_transformed = [scaler.transform(s) for s in series]
    else:
        series_transformed = series

    # Split in the same way as in training
    ## series
    series_split = split_dataset(
            series,
            val_start_date_str=cut_date_val,
            test_start_date_str=cut_date_test,
            test_end_date=test_end_date,
            multiple=multiple,
            country_l=country_l,
            country_code_l=country_code_l)

    series_transformed_split = split_dataset(
            series_transformed,
            val_start_date_str=cut_date_val,
            test_start_date_str=cut_date_test,
            test_end_date=test_end_date,
            multiple=multiple,
            country_l=country_l,
            country_code_l=country_code_l)


    if multiple:
        eval_i = country_l.index(eval_country)
    else:
        eval_i = 0
    # Evaluate Model

    backtest_series = darts.timeseries.concatenate([series_split['train'][eval_i], series_split['val'][eval_i]]) if multiple else \
                      darts.timeseries.concatenate([series_split['train'], series_split['val']])
    backtest_series_transformed = darts.timeseries.concatenate([series_transformed_split['train'], series_transformed_split['val']])

    validation_results = backtester(model=model,
                                    series_transformed=backtest_series_transformed,
                                    series=backtest_series,
                                    transformer_ts=scaler,
                                    test_start_date=cut_date_val,
                                    forecast_horizon=forecast_horizon,
                                    stride=stride,
                                    retrain=retrain,
                                    future_covariates=future_covariates,
                                    past_covariates=past_covariates,
                                    )

    return validation_results["metrics"]

@click.command()
# load arguments
@click.option("--series-uri",
    default="online_artifact",
    help="Online link to download the series from"
    )

@click.option("--future-covs-uri",
              type=str,
              default='mlflow_artifact_uri'
)
@click.option('--year-range',
    default="2009-2019",
    type=str,
    help='Choose year range to include in the dataset.'
)

@click.option("--resolution",
    default="15",
    type=str,
    help="Change the resolution of the dataset (minutes)."
)
@click.option(
    "--time-covs",
    default="PT",
    type=click.Choice(["None", "PT"]),
    help="Optionally add time covariates to the timeseries."
)
# training arguments
@click.option("--darts-model",
              type=click.Choice(
                  ['NBEATS',
                   'RNN',
                   'TCN',
                   'BlockRNN',
                   'TFT',
                   'LightGBM',
                   'RandomForest',
                   'Naive',
                   'AutoARIMA']),
              multiple=False,
              default='RNN',
              help="The base architecture of the model to be trained"
              )
@click.option("--hyperparams-entrypoint", "-h",
              type=str,
              default='LSTM1',
              help=""" The entry point of config.yml under the 'hyperparams'
              one containing the desired hyperparameters for the selected model"""
              )
@click.option("--cut-date-val",
              type=str,
              default='20190101',
              help="Validation set start date [str: 'YYYYMMDD']"
              )
@click.option("--cut-date-test",
              type=str,
              default='20200101',
              help="Test set start date [str: 'YYYYMMDD']",
              )
@click.option("--test-end-date",
              type=str,
              default='None',
              help="Test set ending date [str: 'YYYYMMDD']",
              )
@click.option("--device",
              type=click.Choice(
                  ['gpu',
                   'cpu']),
              multiple=False,
              default='gpu',
              )
# eval
@click.option("--forecast-horizon",
              type=str,
              default="96")
@click.option("--stride",
              type=str,
              default="None")
@click.option("--retrain",
              type=str,
              default="false",
              help="Whether to retrain model during backtesting")
@click.option("--scale",
              type=str,
              default="true",
              help="Whether to scale the target series")
@click.option("--scale-covs",
              type=str,
              default="true",
              help="Whether to scale the covariates")
@click.option("--multiple",
              type=str,
              default="false",
              help="Whether to train on multiple timeseries")
@click.option("--eval-country",
              type=str,
              default="Portugal",
              help="On which country to run the backtesting. Only for multiple timeseries")
@click.option("--n-trials",
              type=str,
              default="100",
              help="How many trials optuna will run")

def optuna_search(series_uri, future_covs_uri, year_range, resolution, time_covs, darts_model, hyperparams_entrypoint,
           cut_date_val, cut_date_test, test_end_date, device, forecast_horizon, stride, retrain,
           scale, scale_covs, multiple, eval_country, n_trials):

        n_trials = none_checker(n_trials)
        n_trials = int(n_trials)
        with mlflow.start_run(run_name=f'optuna_test_{darts_model}', nested=True) as mlrun:

            study = optuna.create_study(storage="sqlite:///memory.db", study_name=hyperparams_entrypoint, load_if_exists=True)

            opt_tmpdir = tempfile.mkdtemp()

            study.optimize(lambda trial: objective(series_uri, future_covs_uri, year_range, resolution, time_covs,
                       darts_model, hyperparams_entrypoint, cut_date_val, test_end_date, cut_date_test, device,
                       forecast_horizon, stride, retrain, scale, scale_covs,
                       multiple, eval_country, mlrun, trial, study, opt_tmpdir), n_trials=n_trials, n_jobs = 1)

            log_optuna(study, opt_tmpdir, hyperparams_entrypoint, mlrun)

            return

if __name__ =='__main__':
    print("\n=========== OPTUNA SEARCH =============")
    logging.info("\n=========== OPTUNA SEARCH =============")
    mlflow.tracking.MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)
    print("Current tracking uri: {}".format(mlflow.get_tracking_uri()))
    logging.info("Current tracking uri: {}".format(mlflow.get_tracking_uri()))
    optuna_search()
