import errno
import logging
import os
from datetime import datetime
from typing import List

from py4j.protocol import Py4JJavaError
from pyspark.sql import DataFrame, functions
from pyspark.sql.utils import AnalysisException

import listenbrainz_spark
from hdfs.util import HdfsError
from listenbrainz_spark import config, hdfs_connection, path
from listenbrainz_spark.schema import listens_new_schema
from listenbrainz_spark.exceptions import (DataFrameNotAppendedException,
                                           DataFrameNotCreatedException,
                                           FileNotFetchedException,
                                           FileNotSavedException,
                                           HDFSDirectoryNotDeletedException,
                                           PathNotFoundException,
                                           ViewNotRegisteredException)

logger = logging.getLogger(__name__)

# A typical listen is of the form:
# {
#   "artist_mbids": [],
#   "artist_name": "Cake",
#   "listened_at": "2005-02-28T20:39:08Z",
#   "recording_msid": "c559b2f8-41ff-4b55-ab3c-0b57d9b85d11",
#   "recording_mbid": "1750f8ca-410e-4bdc-bf90-b0146cb5ee35",
#   "release_mbid": "",
#   "release_name": null,
#   "tags": [],
#   "track_name": "Tougher Than It Is"
#   "user_id": 5,
# }
# All the keys in the dict are column/field names in a Spark dataframe.


def append(df, dest_path):
    """ Append a dataframe to existing dataframe in HDFS or write a new one
        if dataframe does not exist.

        Args:
            df (dataframe): Dataframe to append.
            dest_path (string): Path where the existing dataframe is found or
                                where a new dataframe should be created.
    """
    try:
        df.write.mode('append').parquet(config.HDFS_CLUSTER_URI + dest_path)
    except Py4JJavaError as err:
        raise DataFrameNotAppendedException(err.java_exception, df.schema)


def create_dataframe(row, schema):
    """ Create a dataframe containing a single row.

        Args:
            row (pyspark.sql.Row object): A Spark SQL row.
            schema: Dataframe schema.

        Returns:
            df (dataframe): Newly created dataframe.
    """
    try:
        df = listenbrainz_spark.session.createDataFrame([row], schema=schema)
        return df
    except Py4JJavaError as err:
        raise DataFrameNotCreatedException(err.java_exception, row)


def create_path(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def register_dataframe(df, table_name):
    """ Creates a view to be used for Spark SQL, etc. Replaces the view if a view with the
        same name exists.

        Args:
            df (dataframe): Dataframe to register.
            table_name (str): Name of the view.
    """
    try:
        df.createOrReplaceTempView(table_name)
    except Py4JJavaError as err:
        raise ViewNotRegisteredException(err.java_exception, table_name)


def read_files_from_HDFS(path):
    """ Loads the dataframe stored at the given path in HDFS.

        Args:
            path (str): An HDFS path.
    """
    # if we point spark to a directory, it will read each file in the directory as a
    # parquet file and return the dataframe. so if a non-parquet file in also present
    # in the same directory, we will get the not a parquet file error
    try:
        df = listenbrainz_spark.sql_context.read.parquet(config.HDFS_CLUSTER_URI + path)
        return df
    except AnalysisException as err:
        raise PathNotFoundException(str(err), path)
    except Py4JJavaError as err:
        raise FileNotFetchedException(err.java_exception, path)


def get_listen_files_list() -> List[str]:
    """ Get list of name of parquet files containing the listens.
    The list of file names is in order of newest to oldest listens.
    """
    files = hdfs_connection.client.list(path.LISTENBRAINZ_NEW_DATA_DIRECTORY)
    has_incremental = False
    file_names = []

    for file in files:
        # handle incremental dumps separately because later we want to sort
        # based on numbers in file name
        if file == "incremental.parquet":
            has_incremental = True
            continue
        if file.endswith(".parquet"):
            file_names.append(file)

    # parquet files which come from full dump are named as 0.parquet, 1.parquet so
    # on. listens are stored in ascending order of listened_at. so higher the number
    # in the name of the file, newer the listens. Therefore, we sort the list
    # according to numbers in name of parquet files, in reverse order to start
    # loading newer listens first.
    file_names.sort(key=lambda x: int(x.split(".")[0]), reverse=True)

    # all incremental dumps are stored in incremental.parquet. these are the newest
    # listens. but an incremental dump might not always exist for example at the time
    # when a full dump has just been imported. so check if incremental dumps are
    # present, if yes then add those to the start of list
    if has_incremental:
        file_names.insert(0, "incremental.parquet")

    return file_names


def get_listens_from_new_dump(start: datetime, end: datetime) -> DataFrame:
    """ Load listens with listened_at between from_ts and to_ts from HDFS in a spark dataframe.

        Args:
            start: minimum time to include a listen in the dataframe
            end: maximum time to include a listen in the dataframe

        Returns:
            dataframe of listens with listened_at between start and end
    """
    files = get_listen_files_list()

    # create empty dataframe for merging loaded files into it
    dfs = listenbrainz_spark.session.createDataFrame([], listens_new_schema)

    for file_name in files:
        df = read_files_from_HDFS(
            os.path.join(path.LISTENBRAINZ_NEW_DATA_DIRECTORY, file_name)
        )

        # check if the currently loaded file has any listens newer than the starting
        # timestamp. if not stop trying to load more files, because listens are sorted
        # by listened_at in ascending order and we are traversing the files in reverse
        # order. that is we are loading listens from latest to oldest so if the current
        # file does not have any listens newer than from_ts, the remaining files will
        # not have those either.
        df = df.where(f"listened_at >= to_timestamp('{start}')")
        if df.count() == 0:
            break

        # cannot merge this condition with the above one because, consider the following case:
        # we want listens between the time range - 14 days ago to 7 days ago. the latest file
        # might have listens only from last 4 days. if the conditions were merged, we would
        # have stopped looking in other files which is wrong. it is quite possible that the 2nd
        # or some subsequent file has listens older than to_ts but newer than from_ts
        df = df.where(f"listened_at <= to_timestamp('{end}')")

        dfs = dfs.union(df)

    return dfs


def get_latest_listen_ts() -> datetime:
    """" Get the listened_at time of the latest listen present
     in the imported dumps
     """
    latest_listen_file = get_listen_files_list()[0]
    df = read_files_from_HDFS(
        os.path.join(path.LISTENBRAINZ_NEW_DATA_DIRECTORY, latest_listen_file)
    )
    return df \
        .select('listened_at') \
        .agg(functions.max('listened_at').alias('latest_listen_ts'))\
        .collect()[0]['latest_listen_ts']


def save_parquet(df, path, mode='overwrite'):
    """ Save dataframe as parquet to given path in HDFS.

        Args:
            df (dataframe): Dataframe to save.
            path (str): Path in HDFS to save the dataframe.
            mode (str): The mode with which to write the paquet.
    """
    try:
        df.write.format('parquet').save(config.HDFS_CLUSTER_URI + path, mode=mode)
    except Py4JJavaError as err:
        raise FileNotSavedException(err.java_exception, path)


def read_json(hdfs_path, schema):
    """ Upload JSON file to HDFS as parquet.

        Args:
            hdfs_path (str): HDFS path to upload JSON.
            schema: Blueprint of parquet.

        Returns:
            df (parquet): Dataframe.
    """
    df = listenbrainz_spark.session.read.json(config.HDFS_CLUSTER_URI + hdfs_path, schema=schema)
    return df
