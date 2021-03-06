import re
import gzip
from copy import deepcopy
from pathlib import Path
from multiprocessing import Process, Queue
from typing import Optional, Union

import pandas as pd

from log_analyser.log_tools import get_file_md5_hash


# TODO: add logging
class LogDataFrame:
    def __init__(self, config: dict, df: Optional[pd.DataFrame] = None) -> None:
        self._config = deepcopy(config)
        self._df: pd.DataFrame
        self._hashes: list

        config_df_columns: list = self._config['log_dataframe_columns']

        log_dir = Path(config['log_dir']).resolve() if config['log_dir'] else None
        pickle_file = Path(config['pickle_file']).resolve() if config['pickle_file'] else None
        hashes_file = Path(config['hashes_file']).resolve() if config['hashes_file'] else None

        self._config['log_dir'] = log_dir
        self._config['pickle_file'] = pickle_file
        self._config['hashes_file'] = hashes_file

        if df is not None:
            df_columns = list(df.columns)
            if set(df_columns) == set(config_df_columns):
                self._df = df
            else:
                raise ValueError(f'Unmatching data frame columns sets\n'
                                 f'\tdata frame columns: {str(df_columns)}\n'
                                 f'\tconfig columns: {str(config_df_columns)}')
        elif pickle_file and pickle_file.is_file():
            self._df = pd.read_pickle(str(pickle_file))
        else:
            print('Created new data frame')
            self._df = pd.DataFrame(columns=config_df_columns)

        if hashes_file and hashes_file.is_file():
            with hashes_file.open('r') as f:
                f_read = f.read()
                self._hashes = f_read.split('\n') if f_read else []
        else:
            self._hashes = []

    @staticmethod
    def _read_file(log_file: Path) -> str:
        if re.fullmatch(r'.+\.gz$', log_file.name):
            with gzip.open(log_file, 'rb') as f:
                log_bytes: bytes = f.read()
                log_str = log_bytes.decode()
        else:
            with log_file.open('r') as f:
                log_str = f.read()

        return log_str

    # TODO: add column names passing to transformer via decorator
    @staticmethod
    def _apply_to_df(df: pd.DataFrame, config: dict) -> pd.DataFrame:
        for df_transformer in config['transform']:
            column = df_transformer['column']
            transformer = df_transformer['transformer']
            df.loc[:, column] = df.apply(transformer, axis=1)

        return df

    @staticmethod
    def _filter_df(df: pd.DataFrame, config: dict, pre_filters: bool = False) -> pd.DataFrame:
        filters = 'pre_filters' if pre_filters else 'post_filters'

        # further '== True/False' instead of 'is True/False' is for pandas dark magic

        # filter_match
        for df_filter in config[filters]['filter_match']:
            column = df_filter['column']
            regexp = df_filter['regexp']
            if column in df.columns:
                df = df[df[column].str.match(regexp) == True]

        # filter_not_match
        for df_filter in config[filters]['filter_not_match']:
            column = df_filter['column']
            regexp = df_filter['regexp']
            if column in df.columns:
                df = df[df[column].str.match(regexp) == False]

        # filter_in
        for df_filter in config[filters]['filter_in']:
            column = df_filter['column']
            values = df_filter['values']
            if column in df.columns:
                df = df[df[column].isin(values) == True]

        # filter_not_in
        for df_filter in config[filters]['filter_not_in']:
            column = df_filter['column']
            values = df_filter['values']
            if column in df.columns:
                df = df[df[column].isin(values) == False]

        return df

    @staticmethod
    def _process_df(df: pd.DataFrame, config: dict) -> Optional[pd.DataFrame]:
        df = LogDataFrame._filter_df(df.copy(), config, True)
        if df.shape[0] > 0:
            df = LogDataFrame._apply_to_df(df.copy(), config)
            df = LogDataFrame._filter_df(df.copy(), config)
            return df
        else:
            return None

    # log data frame processing is isolated to separate process because of Pandas memory leaks
    def _wrap_process_df(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        def wrap_with_queue(in_queue: Queue, out_queue: Queue) -> None:
            df_in, config_in = in_queue.get()
            df_out = self._process_df(df_in, config_in)
            out_queue.put(df_out)

        q_in = Queue()
        q_out = Queue()

        p = Process(target=wrap_with_queue, args=(q_in, q_out))
        p.start()

        q_in.put((df, self._config))
        processed_df = q_out.get()

        return processed_df

    def _update_from_files(self, log_dir: Path) -> Optional[pd.DataFrame]:
        df_update: Optional[pd.DataFrame] = None
        update_files = {}

        for log_file in log_dir.glob(self._config['log_file_name_glob_pattern']):
            file_hash = get_file_md5_hash(log_file)

            if file_hash not in self._hashes:
                update_files[file_hash] = log_file

        for log_file in update_files.values():
            print(f'Processing file {str(log_file)}')
            log_str = self._read_file(log_file)
            parsed = re.findall(self._config['log_source_pattern'], log_str, flags=re.MULTILINE)

            if parsed:
                df_parsed = pd.DataFrame(data=parsed, columns=self._config['log_source_fields'])
                df_processed = self._wrap_process_df(df_parsed)
            else:
                continue

            if df_processed is not None:
                df_columns = list(self._df.columns)
                df_processed_columns = list(df_processed.columns)

                if set(df_columns) != set(df_processed_columns):
                    raise ValueError(f'Unmatching data frame columns sets\n'
                                     f'\tdata frame columns: {str(df_columns)}\n'
                                     f'\tappending columns: {str(df_processed_columns)}')

                if df_update is None:
                    df_update = df_processed
                else:
                    df_update = df_update.append(other=df_processed, ignore_index=True)

        self._df = self._df.append(other=df_update, ignore_index=True)
        self._df.drop_duplicates(inplace=True)

        if update_files:
            pickle_file = self._config['pickle_file']
            hashes_file = self._config['hashes_file']

            if pickle_file:
                self._df.to_pickle(str(pickle_file))

            if hashes_file:
                self._hashes.extend(list(update_files.keys()))
                with hashes_file.open('w') as f:
                    f.write('\n'.join(self._hashes))

        return df_update

    def _update_from_df(self, df_update: pd.DataFrame) -> Optional[pd.DataFrame]:
        df_processed = self._wrap_process_df(df_update)

        if df_processed is not None:
            df_columns = list(self._df.columns)
            df_processed_columns = list(df_processed.columns)

            if set(df_columns) != set(df_processed_columns):
                raise ValueError(f'Unmatching data frame columns sets\n'
                                 f'\tdata frame columns: {str(df_columns)}\n'
                                 f'\tappending columns: {str(df_processed_columns)}')

            self._df = self._df.append(other=df_processed, ignore_index=True)
            self._df.drop_duplicates(inplace=True)

            pickle_file = self._config['pickle_file']
            if pickle_file:
                self._df.to_pickle(str(pickle_file))

        return df_processed

    def update(self, source: Optional[Union[str, Path, pd.DataFrame]] = None) -> Optional[pd.DataFrame]:
        if source is None:
            result = self._update_from_files(self._config['log_dir'])
        elif isinstance(source, (str, Path)):
            result = self._update_from_files(Path(source).resolve())
        elif isinstance(source, pd.DataFrame):
            result = self._update_from_df(source)
        else:
            result = None

        return result

    def df(self) -> pd.DataFrame:
        return self._df
