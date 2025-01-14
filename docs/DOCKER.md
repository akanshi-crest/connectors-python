# Run Connector Service in Docker

To run the Connector Service in Docker, you need to have Docker installed locally.

This guide uses generally-available unix commands to demonstrate how to run the Connector Service in Docker.

Windows users might have to run them in [Unix Subsystem](https://learn.microsoft.com/en-us/windows/wsl/about), rewrite the commands in PowerShell, or execute them manually.

Follow these steps:

1. [Create directory](#1-create-a-directory-to-be-mounted-into-the-docker-image)
2. [Download config file](#2-download-sample-configuration-file-from-this-repository-into-newly-created-directory)
3. [Update config file](#3-update-the-configuration-file-for-your-on-prem-connector)
4. [Run the docker image](#4-run-the-docker-image)

## 1. Create a directory to be mounted into the Docker image.

This directory will contain the configuration file used to run the Connector Service. The examples in this guide will use the user's home directory (`~`).

```sh
cd ~ && mkdir connectors-python-config
```

## 2. Download sample configuration file from this repository into newly created directory.

You can download the file manually, or simply run the command below. Make sure to update the `--output` argument value if your directory name is different,  or you want to use a different config file name.

```sh
curl https://raw.githubusercontent.com/elastic/connectors-python/main/config.yml --output ~/connectors-python-config/config.yml
```

## 3. Update the configuration file for your [on-prem connector](https://www.elastic.co/guide/en/enterprise-search/current/build-connector.html#build-connector-usage)

If you're running the Connector Service against a dockerised version of Elasticsearch and Kibana, your config file will look like this:

```
elasticsearch:
  host: http://host.docker.internal:9200
  username: elastic
  password: <YOUR_PASSWORD>
  ssl: true
  bulk:
    queue_max_size: 1024
    queue_max_mem_size: 25
    display_every: 100
    chunk_size: 1000
    max_concurrency: 5
    chunk_max_mem_size: 5
    concurrent_downloads: 10
  request_timeout: 120
  max_wait_duration: 120
  initial_backoff_duration: 1
  backoff_multiplier: 2
  log_level: info

service:
  idling: 30
  heartbeat: 300
  max_errors: 20
  max_errors_span: 600
  max_concurrent_syncs: 1
  job_cleanup_interval: 300
  log_level: INFO
  
# connector information
connector_id: <CONNECTOR_ID_FROM_KIBANA>
service_type: <DESIRED_SERVICE_TYPE>

sources:
  mongodb: connectors.sources.mongo:MongoDataSource
  s3: connectors.sources.s3:S3DataSource
  dir: connectors.sources.directory:DirectoryDataSource
  mysql: connectors.sources.mysql:MySqlDataSource
  network_drive: connectors.sources.network_drive:NASDataSource
  google_cloud_storage: connectors.sources.google_cloud_storage:GoogleCloudStorageDataSource
  azure_blob_storage: connectors.sources.azure_blob_storage:AzureBlobStorageDataSource
  postgresql: connectors.sources.postgresql:PostgreSQLDataSource
  oracle: connectors.sources.oracle:OracleDataSource
  mssql: connectors.sources.mssql:MSSQLDataSource
```

Notice, that the config file you downloaded might contain more config entries, so you will need to manually copy/change the settings that apply to you. It should be sufficient to only update `elasticsearch.host`, `elasticsearch.password`, `connector_id` and `service_type` to make Connectors Service run properly for you.

### Running connector service in [native mode](https://www.elastic.co/guide/en/enterprise-search/current/native-connectors.html) in Docker

To run the connector service in native mode, you will need a slightly different configuration file. The `native_service_types` config option needs to be populated while `connector_id` and `service_type` settings should be removed or commented out. Normally, when you download a sample configuration file per step 2, you'll be able to run the Connector Service in native mode (as long as the Elasticsearch host and credentials are correct).

Here is an example configuration file for a Connector Service running in native mode:

```
elasticsearch:
  host: http://host.docker.internal:9200
  username: elastic
  password: <YOUR_PASSWORD>
  ssl: true
  bulk:
    queue_max_size: 1024
    queue_max_mem_size: 25
    display_every: 100
    chunk_size: 1000
    max_concurrency: 5
    chunk_max_mem_size: 5
    concurrent_downloads: 10
  request_timeout: 120
  max_wait_duration: 120
  initial_backoff_duration: 1
  backoff_multiplier: 2
  log_level: info

service:
  idling: 30
  heartbeat: 300
  max_errors: 20
  max_errors_span: 600
  max_concurrent_syncs: 1
  job_cleanup_interval: 300
  log_level: INFO
  
# remove entries from this list to not run these connectors in this instance of the service
native_service_types:
  - mongodb
  - mysql
  - network_drive
  - s3
  - google_cloud_storage
  - azure_blob_storage
  - postgresql
  - oracle
  - dir
  - mssql

sources:
  mongodb: connectors.sources.mongo:MongoDataSource
  s3: connectors.sources.s3:S3DataSource
  dir: connectors.sources.directory:DirectoryDataSource
  mysql: connectors.sources.mysql:MySqlDataSource
  network_drive: connectors.sources.network_drive:NASDataSource
  google_cloud_storage: connectors.sources.google_cloud_storage:GoogleCloudStorageDataSource
  azure_blob_storage: connectors.sources.azure_blob_storage:AzureBlobStorageDataSource
  postgresql: connectors.sources.postgresql:PostgreSQLDataSource
  oracle: connectors.sources.oracle:OracleDataSource
  mssql: connectors.sources.mssql:MSSQLDataSource
```

After that, you can build your own Docker image to run:

```
docker build -t <TAG_OF_THE_IMAGE> .
```

For example, if you've created a custom version of MongoDB connector, you can tag it with the following command:

```
docker build -t connector/custom-mongodb:1.0 .
```

You can later use `<TAG_OF_THE_IMAGE>` instead of `docker.elastic.co/enterprise-search/elastic-connectors:8.7.0.0-SNAPSHOT` in the next step to run the Docker image.

## 4. Run the Docker image.

Now you can run the Docker image with the Connector Service. Here's an example command:

```sh
docker run \
-v ~/connectors-python-config:/config \
--network "elastic" \
--tty \
--rm \
docker.elastic.co/enterprise-search/elastic-connectors:8.7.0.0-SNAPSHOT \
/app/bin/elastic-ingest \
-c /config/config.yml
```

You might need to adjust some details here:
- `-v ~/connectors-python-config:/config \` - replace `~/connectors-python-config` with the directory that you've created in step 1 if you've chosen a different name for it.
- `docker.elastic.co/enterprise-search/elastic-connectors:8.7.0.0-SNAPSHOT` - adjust the version for the connectors to match your Elasticsearch deployment version. 
  - For Elasticsearch of version 8.7 you can use `elastic-connectors:8.7.0.0`for a stable revision of the connectors, or `elastic-connectors:8.7.0.0-SNAPSHOT` if you want the latest nightly build of the connectors (not recommended).
  - If you are using nightly builds, you will need to run `docker pull docker.elastic.co/enterprise-search/elastic-connectors:8.7.0.0-SNAPSHOT` before starting the service. This ensures you're using the latest version of the Docker image.
- `-c /config/config.yml` - replace `config.yml` with the name of the config file you've put in the directory you've created in step 1. 
  - Normally, if you run a single instance of the Connector Service you can use `config.yml`. 
  - If you're planning to run multiple versions of the service, you can put configs for different service versions in a single directory and update this setting to point to different config files.
