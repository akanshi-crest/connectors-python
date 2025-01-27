#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""SharePoint source module responsible to fetch documents from SharePoint Server/Online.
"""
import asyncio
import os
from datetime import datetime
from functools import partial
from urllib.parse import urljoin

import aiofiles
import aiohttp
from aiofiles.os import remove
from aiofiles.tempfile import NamedTemporaryFile
from aiohttp.client_exceptions import ClientResponseError, ServerDisconnectedError

from connectors.logger import logger
from connectors.source import BaseDataSource
from connectors.utils import (
    TIKA_SUPPORTED_FILETYPES,
    CancellableSleeps,
    RetryStrategy,
    convert_to_b64,
    evaluate_timedelta,
    is_expired,
    retryable,
    ssl_context,
    url_encode,
)

RETRY_INTERVAL = 2
RETRIES = 3
FILE_SIZE_LIMIT = 10485760
CHUNK_SIZE = 1024
TOP = 5000
PING = "ping"
SITES = "sites"
LISTS = "lists"
ATTACHMENT = "attachment"
DRIVE_ITEM = "drive_item"
LIST_ITEM = "list_item"
ATTACHMENT_DATA = "attachment_data"
DOCUMENT_LIBRARY = "document_library"

URLS = {
    PING: "{host_url}/sites/{site_collections}/_api/web/webs",
    SITES: "{host_url}{parent_site_url}/_api/web/webs?$skip={skip}&$top={top}",
    LISTS: "{host_url}{parent_site_url}/_api/web/lists?$skip={skip}&$top={top}&$expand=RootFolder&$filter=(Hidden eq false)",
    ATTACHMENT: "{host_url}{value}/_api/web/GetFileByServerRelativeUrl('{file_relative_url}')/$value",
    DRIVE_ITEM: "{host_url}{parent_site_url}/_api/web/lists(guid'{list_id}')/items?$select=Modified,Id,GUID,File,Folder&$expand=File,Folder&$top={top}",
    LIST_ITEM: "{host_url}{parent_site_url}/_api/web/lists(guid'{list_id}')/items?$expand=AttachmentFiles&$select=*,FileRef",
    ATTACHMENT_DATA: "{host_url}{parent_site_url}/_api/web/getfilebyserverrelativeurl('{file_relative_url}')",
}
SCHEMA = {
    SITES: {
        "title": "Title",
        "url": "Url",
        "_id": "Id",
        "server_relative_url": "ServerRelativeUrl",
        "_timestamp": "LastItemModifiedDate",
        "creation_time": "Created",
    },
    LISTS: {
        "title": "Title",
        "parent_web_url": "ParentWebUrl",
        "_id": "Id",
        "_timestamp": "LastItemModifiedDate",
        "creation_time": "Created",
    },
    DOCUMENT_LIBRARY: {
        "title": "Title",
        "parent_web_url": "ParentWebUrl",
        "_id": "Id",
        "_timestamp": "LastItemModifiedDate",
        "creation_time": "Created",
    },
    LIST_ITEM: {
        "title": "Title",
        "author_id": "EditorId",
        "creation_time": "Created",
        "_timestamp": "Modified",
    },
    DRIVE_ITEM: {
        "title": "Name",
        "creation_time": "TimeCreated",
        "_timestamp": "TimeLastModified",
    },
}

SHAREPOINT_ONLINE = "sharepoint_online"
SHAREPOINT_SERVER = "sharepoint_server"


class SharepointClient:
    """SharePoint client to handle API calls made to SharePoint"""

    def __init__(self, configuration):
        self._sleeps = CancellableSleeps()
        self.configuration = configuration
        self.is_cloud = self.configuration["data_source"] == SHAREPOINT_ONLINE
        self.host_url = self.configuration["host_url"]
        self.certificate = self.configuration["ssl_ca"]
        self.ssl_enabled = self.configuration["ssl_enabled"]
        self.retry_count = self.configuration["retry_count"]
        self.site_collections = self.configuration["site_collections"]

        self.session = None
        self.access_token = None
        self.token_expires_at = None

        if self.ssl_enabled and self.certificate:
            self.ssl_ctx = ssl_context(certificate=self.certificate)
        else:
            self.ssl_ctx = False

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def _set_access_token(self):
        """Set access token using configuration fields"""
        expires_at = self.token_expires_at
        if self.token_expires_at and (not isinstance(expires_at, datetime)):
            expires_at = datetime.fromisoformat(expires_at)  # pyright: ignore
        if not is_expired(expires_at=expires_at):
            return
        tenant_id = self.configuration["tenant_id"]
        logger.debug("Generating access token")
        url = f"https://accounts.accesscontrol.windows.net/{tenant_id}/tokens/OAuth/2"
        # GUID in resource is always a constant used to create access token
        data = {
            "grant_type": "client_credentials",
            "resource": f"00000003-0000-0ff1-ce00-000000000000/{self.configuration['tenant']}.sharepoint.com@{tenant_id}",
            "client_id": f"{self.configuration['client_id']}@{tenant_id}",
            "client_secret": self.configuration["client_secret"],
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with aiohttp.request(
            method="POST", url=url, data=data, headers=headers
        ) as response:
            json_data = await response.json()
            self.access_token = json_data["access_token"]
            self.token_expires_at = evaluate_timedelta(
                seconds=int(json_data["expires_in"]), time_skew=20
            )

    def _get_session(self):
        """Generate base client session using configuration fields

        Returns:
            ClientSession: Base client session.
        """
        if self.session:
            return self.session
        logger.info("Generating aiohttp Client Session...")
        request_headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=None)  # pyright: ignore

        if self.is_cloud:
            basic_auth = None
        else:
            basic_auth = aiohttp.BasicAuth(
                login=self.configuration["username"],
                password=self.configuration["password"],
            )
        self.session = aiohttp.ClientSession(
            auth=basic_auth,
            headers=request_headers,
            timeout=timeout,
            raise_for_status=True,
        )
        return self.session

    async def close_session(self):
        """Closes unclosed client session"""
        self._sleeps.cancel()
        if self.session is None:
            return
        await self.session.close()
        self.session = None

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def get_content(
        self, document, file_relative_url, site_url, timestamp=None, doit=False
    ):
        """Get content of list items and drive items

        Args:
            document (dictionary): Modified document.
            file_relative_url (str): Relative url of file
            site_url (str): Site path of SharePoint
            timestamp (timestamp, optional): Timestamp of item last modified. Defaults to None.
            doit (boolean, optional): Boolean value for whether to get content or not. Defaults to False.

        Returns:
            dictionary: Content document with id, timestamp & text.
        """
        if not (doit and document["Length"]):
            return

        document_size = int(document["Length"])
        if document_size > FILE_SIZE_LIMIT:
            logger.warning(
                f"File size {document_size} of file {document['title']} is larger than {FILE_SIZE_LIMIT} bytes. Discarding file content"
            )
            return

        source_file_name = ""

        async with NamedTemporaryFile(mode="wb", delete=False) as async_buffer:
            async for response in self._api_call(
                url_name=ATTACHMENT,
                host_url=self.host_url,
                value=site_url,
                file_relative_url=file_relative_url,
            ):
                async for data in response.content.iter_chunked(CHUNK_SIZE):
                    await async_buffer.write(data)

            source_file_name = async_buffer.name

        await asyncio.to_thread(
            convert_to_b64,
            source=source_file_name,
        )
        async with aiofiles.open(file=source_file_name, mode="r") as target_file:
            # base64 on macOS will add a EOL, so we strip() here
            attachment_content = (await target_file.read()).strip()
        await remove(source_file_name)  # pyright: ignore
        return {
            "_id": document.get("id"),
            "_timestamp": document.get("_timestamp"),
            "_attachment": attachment_content,
        }

    async def _api_call(self, url_name, url="", **url_kwargs):
        """Make an API call to the SharePoint Server/Online

        Args:
            url_name (str): SharePoint url name to be executed.
            url(str, optional): Paginated url for drive and list items. Defaults to "".
            url_kwargs (dict): Url kwargs to format the query.
        Raises:
            exception: An instance of an exception class.

        Yields:
            data: API response.
        """
        retry = 0
        # If pagination happens for list and drive items then next pagination url comes in response which will be passed in url field.
        if url == "":
            url = URLS[url_name].format(**url_kwargs)

        headers = None

        while True:
            try:
                if self.is_cloud:
                    await self._set_access_token()
                    headers = {"Authorization": f"Bearer {self.access_token}"}
                async with self._get_session().get(  # pyright: ignore
                    url=url,
                    ssl=self.ssl_ctx,
                    headers=headers,
                ) as result:
                    if url_name == ATTACHMENT:
                        yield result
                    else:
                        yield await result.json()
                    break
            except Exception as exception:
                if isinstance(
                    exception,
                    ClientResponseError,
                ) and "token has expired" in exception.headers.get(  # pyright: ignore
                    "x-ms-diagnostics", ""
                ):
                    await self._set_access_token()
                elif isinstance(
                    exception,
                    ServerDisconnectedError,
                ):
                    await self.close_session()
                if retry >= self.retry_count:
                    raise exception
                retry += 1

                logger.warning(
                    f"Retry count: {retry} out of {self.retry_count}. Exception: {exception}"
                )
                await self._sleeps.sleep(RETRY_INTERVAL**retry)

    async def _fetch_data_with_next_url(self, site_url, list_id, param_name):
        """Invokes a GET call to the SharePoint Server/Online for calling list and drive item API.

        Args:
            site_url(string): site url to the SharePoint farm.
            list_id(string): Id of list item or drive item.
            param_name(string): parameter name whether it is DRIVE_ITEM, LIST_ITEM.
        Yields:
            Response of the GET call.
        """
        next_url = ""
        while True:
            if next_url != "":
                response = await anext(
                    self._api_call(
                        url_name=param_name,
                        url=next_url,
                    )
                )
            else:
                response = await anext(
                    self._api_call(
                        url_name=param_name,
                        parent_site_url=site_url,
                        list_id=list_id,
                        top=TOP,
                        host_url=self.host_url,
                    )
                )
            response_result = response.get("value", [])  # pyright: ignore
            yield response_result

            next_url = response.get("odata.nextLink", "")  # pyright: ignore
            if next_url == "":
                break

    async def _fetch_data_with_query(self, site_url, param_name):
        """Invokes a GET call to the SharePoint Server/Online for calling site and list API.

        Args:
            site_url(string): site url to the SharePoint farm.
            param_name(string): parameter name whether it is SITES, LISTS.
        Yields:
            Response of the GET call.
        """
        skip = 0
        while True:
            response = await anext(
                self._api_call(
                    url_name=param_name,
                    parent_site_url=site_url,
                    skip=skip,
                    top=TOP,
                    host_url=self.host_url,
                )
            )
            response_result = response.get("value", [])  # pyright: ignore
            yield response_result

            skip += TOP
            if len(response_result) < TOP:
                break

    async def get_sites(self, site_url):
        """Get sites from SharePoint Server/Online

        Args:
            site_url(string): Parent site relative path.
        Yields:
            site_server_url(string): Site path.
        """
        async for sites_data in self._fetch_data_with_query(
            site_url=site_url, param_name=SITES
        ):
            for data in sites_data:
                async for sub_site in self.get_sites(  # pyright: ignore
                    site_url=data["ServerRelativeUrl"]
                ):
                    yield sub_site
                yield data

    async def get_lists(self, site_url):
        """Get site lists from SharePoint Server/Online

        Args:
            site_url(string): Parent site relative path.
        Yields:
            list_data(string): Response of list API call
        """
        async for list_data in self._fetch_data_with_query(
            site_url=site_url, param_name=LISTS
        ):
            yield list_data

    async def get_attachment(self, site_url, file_relative_url):
        """Execute the call for fetching attachment metadata

        Args:
            site_url(string): Parent site relative path
            file_relative_url(string): Relative url of file
        Returns:
            attachment_data(dictionary): Attachment metatdata
        """
        return await anext(
            self._api_call(
                url_name=ATTACHMENT_DATA,
                host_url=self.host_url,
                parent_site_url=site_url,
                file_relative_url=file_relative_url,
            )
        )

    async def get_list_items(self, list_id, site_url, server_relative_url):
        """This method fetches items from all the lists in a collection.

        Args:
            list_id(string): List id.
            site_url(string): Site path.
            server_relative_url(string): Relative url of site
        Yields:
            dictionary: dictionary containing meta-data of the list item.
        """
        file_relative_url = None
        async for list_items_data in self._fetch_data_with_next_url(
            site_url=site_url, list_id=list_id, param_name=LIST_ITEM
        ):
            for result in list_items_data:
                if not result.get("Attachments"):
                    url = f"{self.host_url}{server_relative_url}/DispForm.aspx?ID={result['Id']}&Source={self.host_url}{server_relative_url}/AllItems.aspx&ContentTypeId={result['ContentTypeId']}"
                    result["url"] = url
                    yield result, file_relative_url
                    continue

                for attachment_file in result.get("AttachmentFiles"):
                    file_relative_url = url_encode(
                        original_string=attachment_file.get("ServerRelativeUrl")
                    )

                    attachment_data = await self.get_attachment(
                        site_url, file_relative_url
                    )
                    result["Length"] = attachment_data.get("Length")  # pyright: ignore
                    result["_id"] = attachment_data["UniqueId"]  # pyright: ignore
                    result["url"] = urljoin(
                        self.host_url,
                        attachment_file.get("ServerRelativeUrl"),
                    )
                    result["file_name"] = attachment_file.get("FileName")

                    if (
                        os.path.splitext(attachment_file["FileName"])[-1]
                        not in TIKA_SUPPORTED_FILETYPES
                    ):
                        file_relative_url = None

                    yield result, file_relative_url

    async def get_drive_items(self, list_id, site_url, server_relative_url):
        """This method fetches items from all the drives in a collection.

        Args:
            list_id(string): List id.
            site_url(string): Site path.
            server_relative_url(string): Relative url of site
        Yields:
            dictionary: dictionary containing meta-data of the drive item.
        """
        async for drive_items_data in self._fetch_data_with_next_url(
            site_url=site_url, list_id=list_id, param_name=DRIVE_ITEM
        ):
            for result in drive_items_data:
                file_relative_url = None
                item_type = "Folder"

                if result.get("File", {}).get("TimeLastModified"):
                    item_type = "File"
                    file_relative_url = (
                        url_encode(original_string=result["File"]["ServerRelativeUrl"])
                        if os.path.splitext(result["File"]["Name"])[-1]
                        in TIKA_SUPPORTED_FILETYPES
                        else None
                    )
                    result["Length"] = result[item_type]["Length"]
                result["item_type"] = item_type

                yield result, file_relative_url

    async def ping(self):
        """Executes the ping call in async manner"""
        await anext(
            self._api_call(
                url_name=PING,
                site_collections=self.site_collections[0],
                host_url=self.host_url,
            )
        )


class SharepointDataSource(BaseDataSource):
    """SharePoint"""

    name = "SharePoint"
    service_type = "sharepoint"

    def __init__(self, configuration):
        """Setup the connection to the SharePoint

        Args:
            configuration (DataSourceConfiguration): Object of DataSourceConfiguration class.
        """
        super().__init__(configuration=configuration)
        self.sharepoint_client = SharepointClient(configuration=configuration)

    @classmethod
    def get_default_configuration(cls):
        """Get the default configuration for SharePoint

        Returns:
            dictionary: Default configuration.
        """
        return {
            "data_source": {
                "display": "dropdown",
                "label": "SharePoint data source",
                "options": [
                    {"label": "SharePoint Online", "value": SHAREPOINT_ONLINE},
                    {"label": "SharePoint Server", "value": SHAREPOINT_SERVER},
                ],
                "order": 1,
                "type": "str",
                "value": SHAREPOINT_SERVER,
            },
            "username": {
                "depends_on": [{"field": "data_source", "value": SHAREPOINT_SERVER}],
                "label": "SharePoint Server username",
                "order": 2,
                "type": "str",
                "value": "demo_user",
            },
            "password": {
                "depends_on": [{"field": "data_source", "value": SHAREPOINT_SERVER}],
                "label": "SharePoint Server password",
                "sensitive": True,
                "order": 3,
                "type": "str",
                "value": "abc@123",
            },
            "client_id": {
                "depends_on": [{"field": "data_source", "value": SHAREPOINT_ONLINE}],
                "label": "SharePoint Online client id",
                "order": 4,
                "type": "str",
                "value": "",
            },
            "client_secret": {
                "depends_on": [{"field": "data_source", "value": SHAREPOINT_ONLINE}],
                "label": "SharePoint Online secret id",
                "order": 5,
                "type": "str",
                "value": "",
            },
            "tenant": {
                "depends_on": [{"field": "data_source", "value": SHAREPOINT_ONLINE}],
                "label": "SharePoint Online tenant",
                "order": 6,
                "type": "str",
                "value": "",
            },
            "tenant_id": {
                "depends_on": [{"field": "data_source", "value": SHAREPOINT_ONLINE}],
                "label": "SharePoint Online tenant id",
                "order": 7,
                "type": "str",
                "value": "",
            },
            "host_url": {
                "label": "SharePoint host url",
                "order": 8,
                "type": "str",
                "value": "http://127.0.0.1:8491",
            },
            "site_collections": {
                "display": "textarea",
                "label": "Comma-separated list of SharePoint site collections to index",
                "order": 9,
                "type": "list",
                "value": "collection1",
            },
            "ssl_enabled": {
                "display": "toggle",
                "label": "Enable SSL",
                "order": 10,
                "type": "bool",
                "value": False,
            },
            "ssl_ca": {
                "depends_on": [{"field": "ssl_enabled", "value": True}],
                "label": "SSL certificate",
                "order": 11,
                "type": "str",
                "value": "",
            },
            "retry_count": {
                "default_value": RETRIES,
                "display": "numeric",
                "label": "Retries per request",
                "order": 12,
                "required": False,
                "type": "int",
                "ui_restrictions": ["advanced"],
                "value": RETRIES,
            },
        }

    async def close(self):
        """Closes unclosed client session"""
        await self.sharepoint_client.close_session()

    async def ping(self):
        """Verify the connection with SharePoint"""
        try:
            await self.sharepoint_client.ping()
            logger.debug(
                f"Successfully connected to the SharePoint via {self.sharepoint_client.host_url}"
            )
        except Exception:
            logger.exception(
                f"Error while connecting to the SharePoint via {self.sharepoint_client.host_url}"
            )
            raise

    def map_documet_with_schema(
        self,
        document,
        item,
        document_type,
    ):
        """Prepare key mappings for documents

        Args:
            document(dictionary): Modified document
            item (dictionary): Document from SharePoint.
            document_type(string): Type of document(i.e. site,list,list_iitem, drive_item and document_library).

        Returns:
            dictionary: Modified document with the help of adapter schema.
        """
        for elasticsearch_field, sharepoint_field in SCHEMA[document_type].items():
            document[elasticsearch_field] = item[sharepoint_field]

    def format_lists(
        self,
        item,
        document_type,
    ):
        """Prepare key mappings for list

        Args:
            item (dictionary): Document from SharePoint.
            document_type(string): Type of document(i.e. list and document_library).

        Returns:
            dictionary: Modified document with the help of adapter schema.
        """
        document = {"type": document_type}

        document["url"] = urljoin(
            self.sharepoint_client.host_url, item["RootFolder"]["ServerRelativeUrl"]
        )

        self.map_documet_with_schema(
            document=document, item=item, document_type=document_type
        )
        return document

    def format_sites(self, item):
        """Prepare key mappings for site

        Args:
            item (dictionary): Document from SharePoint.

        Returns:
            dictionary: Modified document with the help of adapter schema.
        """
        document = {"type": SITES}

        self.map_documet_with_schema(document=document, item=item, document_type=SITES)
        return document

    def format_drive_item(
        self,
        item,
    ):
        """Prepare key mappings for drive items

        Args:
            item (dictionary): Document from SharePoint.

        Returns:
            dictionary: Modified document with the help of adapter schema.
        """
        document = {"type": DRIVE_ITEM}
        item_type = item["item_type"]

        document.update(
            {
                "_id": item["GUID"],
                "size": item.get("File", {}).get("Length"),
                "url": urljoin(
                    self.sharepoint_client.host_url,
                    item[item_type]["ServerRelativeUrl"],
                ),
                "type": item_type,
            }
        )
        self.map_documet_with_schema(
            document=document, item=item[item_type], document_type=DRIVE_ITEM
        )

        return document

    def format_list_item(
        self,
        item,
    ):
        """Prepare key mappings for list items

        Args:
            item (dictionary): Document from SharePoint.

        Returns:
            dictionary: Modified document with the help of adapter schema.
        """
        document = {"type": LIST_ITEM}

        document.update(
            {
                "_id": item["_id"] if "_id" in item.keys() else item["GUID"],
                "file_name": item.get("file_name"),
                "size": item.get("Length"),
                "url": item["url"],
            }
        )

        self.map_documet_with_schema(
            document=document, item=item, document_type=LIST_ITEM
        )
        return document

    async def get_docs(self, filtering=None):
        """Executes the logic to fetch SharePoint objects in an async manner.

        Yields:
            dictionary: dictionary containing meta-data of the SharePoint objects.
        """

        server_relative_url = []

        for collection in self.sharepoint_client.site_collections:
            server_relative_url.append(f"/sites/{collection}")
            async for site_data in self.sharepoint_client.get_sites(
                site_url=f"/sites/{collection}"
            ):
                server_relative_url.append(site_data["server_relative_url"])
                yield self.format_sites(item=site_data), None

        for site_url in server_relative_url:
            async for list_data in self.sharepoint_client.get_lists(site_url=site_url):
                for result in list_data:
                    # if BaseType value is 1 then it's document library else it's a list
                    if result.get("BaseType") == 1:
                        yield self.format_lists(
                            item=result, document_type=DOCUMENT_LIBRARY
                        ), None
                        server_url = None
                        func = self.sharepoint_client.get_drive_items
                        format_document = self.format_drive_item
                    else:
                        yield self.format_lists(item=result, document_type=LISTS), None
                        server_url = result["RootFolder"]["ServerRelativeUrl"]
                        func = self.sharepoint_client.get_list_items
                        format_document = self.format_list_item

                    async for item, file_relative_url in func(
                        list_id=result.get("Id"),
                        site_url=result.get("ParentWebUrl"),
                        server_relative_url=server_url,
                    ):
                        if file_relative_url is None:
                            yield format_document(item=item), None
                        else:
                            yield format_document(
                                item=item,
                            ), partial(
                                self.sharepoint_client.get_content,
                                item,
                                file_relative_url,
                                site_url,
                            )
