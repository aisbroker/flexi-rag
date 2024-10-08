### Build Index

# partitially based on idea of
#   https://stackoverflow.com/questions/52534211/python-type-hinting-with-db-api/77350678#77350678
from typing import TYPE_CHECKING
import mimetypes

from factory.document_loader_factory import get_document_loaders
from langchain_core.document_loaders import BaseLoader
from rag_index_service.blob_parser_document_loader import BlobParserDocumentLoader
from rag_index_service.tools.default_blob_parser import DefaultBlobParser
from rag_index_service.tools.wget_blob_loader import WgetBlobLoader
if TYPE_CHECKING:
    from _typeshed.dbapi import DBAPIConnection, DBAPICursor
else:
    DBAPIConnection = any
    DBAPICursor = any

import threading
import time
from chromadb import GetResult
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.vectorstores import VectorStore
import shortuuid
from service.configloader import deep_get, settings
from factory.sql_database_factory import get_sql_database_connection
from factory.vectorstore_factory import get_vectorstore
from factory.llm_factory import get_default_embeddings
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStoreRetriever
import logging

from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
)

from utils.hash_util import sha256sum_str
from utils.string_util import str_limit
import queue

logger = logging.getLogger(__name__)

#sqlCon: DBAPIConnection | None = None
sqlCon = None
vectorStore: Optional[VectorStore] = None
vectorStoreRetriever = None

# in-memory queue of downloaded documents to process
downloadedDocumentsToProcessQueue = queue.Queue()

#
# (Persitent) Data Model:
#
# The basic idea is to have documents (table 'document') and 
# # connected document parts (table 'document_part') in the SQL DB.
# These documents and their connected parts can change over time.
#
# And we have a parts (=text snippets + their sha256 hash + their embedding )
# which are stored/cached almost forever to save embedding-calculation costs and time.
# These parts are stored in the 'part' table in the SQL DB
# and in the vectorstore DB.
# Parts are identified and connected by their sha256 hash.
#
# setup if necessary

# a document represents a a full file/document
DB_TABLE_document = """CREATE TABLE IF NOT EXISTS document (
                            id TEXT NOT NULL PRIMARY KEY,
                            source TEXT NOT NULL,
                            content_type TEXT NOT NULL,
                            file_path TEXT,
                            file_size INTEGER,
                            file_sha256 TEXT,
                            last_modified TEXT
                    )"""
# a (document) part represents a part of a document after splitting, e.g., a page, a paragraph, a part of a page
DB_TABLE_part = """CREATE TABLE IF NOT EXISTS part (
                       sha256 TEXT COMMENT "sha256 hash of the content of this part, also used as ID in the vectorstore" NOT NULL PRIMARY KEY,
                       content TEXT NOT NULL
                   )"""
# connection between a document and its parts
DB_TABLE_document_part = """CREATE TABLE IF NOT EXISTS document_part (
                                document_id TEXT NOT NULL,
                                part_sha256 TEXT NOT NULL,
                                anker TEXT COMMENT "position of the part in the document - e.g., page number, paragraph number, ..."
                            )"""




indexing_single_run_counter = 0

def get_indexing_single_run_counter():
    global indexing_single_run_counter
    return indexing_single_run_counter

# main function,
# wait/block until for the first round to finish
def start_indexing():
    # start new thread with endless loop

    # turn-on the worker thread
    threading.Thread(target=indexing_endless_loop_worker, daemon=False).start()

    # wait for the first round to finish
    while get_indexing_single_run_counter() < 1:
        time.sleep(1)

    # first round done
    logger.info("First indexing round done. Indexing is now running in the background.")


def indexing_endless_loop_worker():
    load_every_seconds = deep_get(settings, "config.rag_loading.load_every_seconds")

    while True:
        # preparation
        starttime = time.time()

        # action
        indexing_single_run()

        # finish this round
        now = time.time()
        seconds_until_next_run = int(load_every_seconds - (now - starttime))
        if seconds_until_next_run > 0:
            logger.info(f"Sleeping for {seconds_until_next_run} seconds before starting next indexing round ...")
            time.sleep(seconds_until_next_run)
        else:
            logger.info(f"Indexing round took longer than {load_every_seconds} seconds. Starting next indexing round immediately ...")




def indexing_single_run():
    global indexing_single_run_counter
    logger.info(f"===== indexing_single_run() START (#{indexing_single_run_counter}) =====")

    """
    Here we decouple the crawling/loading and the processing/saving of the downloaded documents
    by using a queue and separated threads.
    """

    # start the worker thread to crawl/load all documents
    threading.Thread(target=download_all_documents_and_put_them_into_queue, args=(), daemon=False).start()

    # process all documents from the queue
    process_all_documents_from_queue_worker()

    # wait until all documents are completely processed
    #downloadedDocumentsToProcessQueue.join()
    # NOT NEEDED: everything will finish by itself when done

    # single run done
    logger.info(f"===== indexing_single_run() RESULTS (#{indexing_single_run_counter}) =====")
    printall()
    logger.info(f"===== indexing_single_run() END (#{indexing_single_run_counter}) =====")
    indexing_single_run_counter += 1


def download_all_documents_and_put_them_into_queue():
    logger.info(f"== download_all_documents_and_put_them_into_queue(): Loading ...")
    document_loaders: List[BaseLoader] = get_document_loaders()
    for document_loader in document_loaders:
        # one (document) loader from the config
        document_loader_info_str = str(document_loader)
        docs = document_loader.lazy_load()
        put_downloaded_documents_into_queue(document_loader_info_str, docs)
    logger.info(f"== download_all_documents_and_put_them_into_queue(): Lazy loading + putting into queue ... {str_limit(docs, 1024)}")

    # Add end signal to queue to finisj this loading round
    downloadedDocumentsToProcessQueue.put(None)

    
def put_downloaded_documents_into_queue(context_str: str, docs: Iterator[Document]):
    logger.info(f"== put_all_downloaded_document_into_queue() - START {context_str} ...")
    counter = 0
    for doc in docs:
        downloadedDocumentsToProcessQueue.put(doc)
        counter += 1
    logger.info(f"== put_all_downloaded_document_into_queue() - END {context_str} ... after {counter} documents")


def process_all_documents_from_queue_worker():
    logger.info("== process_all_documents_in_queue_worker(): Split and save documents in databases - START")

    while True:
        # get next document from queue
        logger.info(f"Next doc from queue: Take it now (queue len={downloadedDocumentsToProcessQueue.qsize()}) ... (blocking) ...")
        doc = downloadedDocumentsToProcessQueue.get()
        if doc is None:
            # end signal
            logger.info("Next doc from queue: No more documents in queue (and no more will come)")
            downloadedDocumentsToProcessQueue.task_done()
            break
        else:
            # normal processing
            logger.info(f"Next doc from queue: Got it")
            doc = _enrich_document(doc)
            process_single_document_and_store_results_in_databases(doc)
            downloadedDocumentsToProcessQueue.task_done()

    logger.info("== process_all_documents_in_queue_worker(): Split and save documents in databases - END")

def _enrich_document(doc: Document) -> Document:
    """
    Enrich a document with metadata.
    """
    # add metadata
    if "content_type" not in doc.metadata:

        source = doc.metadata["source"]
        (mimetype, encoding) = mimetypes.guess_type(source)
        if mimetype is not None:
            doc.metadata["content_type"] = mimetype
        else:
            doc.metadata["content_type"] = "unknown"

    return doc

#
# processing multiple documents
#

#
# Attention: We need to iterate oder configured loaders instead of URLs
# TODO!!!
#
def run_all_document_loaders() -> Iterator[Document]:
    for url in urls:
        document_loader = get_document_loader(url)
        docs_of_single_url = document_loader.lazy_load()
        yield from docs_of_single_url

#
# processing a single document
#

def process_single_document_and_store_results_in_databases(doc: Document):
    """
    Process (load and split) a single document and store (and index) the results in the SQL DB and the vectorstore.

    NOT LAZY: The document is processed and saved in the SQL DB and the vectorstore.
    """

    logger.info(f"process_single_document_and_store_results_in_databases(doc={str_limit(doc)} ...")

    # split doc into parts
    doc_splits = split_single_document_into_parts(doc)
    logger.info(f"process_single_document_and_store_results_in_databases: ")
    for doc_split in doc_splits:
        logger.info(f"  doc_split.metadata={str_limit(doc_split.metadata, 1024)} doc_split.page_content={str_limit(doc_split.page_content   )}")

    # save doc in SQL DB and in vectorstore
    save_single_document_and_its_parts_in_databases(doc, doc_splits)

    logger.info(f"process_single_document_and_store_results_in_databases(doc={str_limit(doc)} ... DONE")


def split_single_document_into_parts(doc: Document) -> List[Document]:
    # Split
    text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=500, chunk_overlap=50
    )
    doc_splits = text_splitter.split_documents([doc])

    # add metadata
    for doc_split in doc_splits:
        # doesn't exist yet: doc_split.metadata["document_id"] = doc.metadata["id"]
        doc_split.metadata["part_sha256"] = sha256sum_str(doc_split.page_content)

    return doc_splits


def save_single_document_and_its_parts_in_databases(doc: Document, doc_parts: Iterator[Document]) -> Tuple[Document, Iterator[Document]]:
    """
    Save a single document and its parts in the SQL DB and the vectorstore.

    NOT LAZY: The document and its parts are processed and saved in the SQL DB and the vectorstore.
    """
    # save doc in SQL DB
    doc_stored = save_docs_in_sqldb([doc])
    doc_stored_done = list(doc_stored)[0]
    document_id = doc_stored_done.metadata["id"]

    # save doc parts in SQL DB and vectorstore
    doc_parts_list = list(doc_parts)
    doc_parts_stored = save_doc_parts_in_vectorstore_and_sqldb(document_id, doc_parts_list)
    # un-lazy
    doc_parts_stored_done = list(doc_parts_stored)

    return doc_stored, doc_parts_stored_done  #doc_parts_stored


# iterate over the documents, save them in SQL DB, and add IDs
def save_docs_in_sqldb(docs_list: Iterator[Document]) -> Iterator[Document]:
    logger.info(f"save_docs_in_sqldb ...")
    
    sqlCon = get_sql_database_connection_after_setup()

    # insert documents (a document represents a a full file/document)
    num = 0
    for doc in docs_list:
        logger.info(f"save_docs_in_sqldb: doc.metadata={str_limit(doc.metadata, 1024)} doc.page_content={str_limit(doc.page_content)}")

        # create uuid and more
        id = "doc-"+str(shortuuid.uuid()[:7])
        #id =       str(shortuuid.uuid()[:7])
        logger.info(f"doc.metadata={doc.metadata}")
        source = doc.metadata['source']
        content_type = doc.metadata.get("content_type")
        file_path = doc.metadata.get("file_path")
        file_size = doc.metadata.get("file_size")
        file_sha256 = doc.metadata.get("file_sha256")
        last_modified = doc.metadata.get("last_modified")

        # Is the source already in the DB? Then delete related entries first
        # Attention: the order of deletion is important!
        cur = sqlCon.cursor()
        cur.execute("DELETE FROM document_part WHERE document_id IN (SELECT id FROM document WHERE source=?)", (source,))
        rowcount = cur.rowcount
        logger.info(f"Deleted {rowcount} row(s) for source={source} from 'document_part' table")
        cur.close()
        
        cur = sqlCon.cursor()
        cur.execute("DELETE FROM document WHERE source=?", (source,))
        rowcount = cur.rowcount
        sqlCon.commit()
        logger.info(f"Deleted {rowcount} row(s) with source={source} from 'document' table")
        cur.close()
        sqlCon.commit()
        
        if False:
            # update row
            logger.info("UPDATE documents SET url = ?, file_path = ?, content = ?, last_modified = ? WHERE id = ?", (doc.metadata.url, doc.page_content, doc.metadata))
        # else
        else:
            # inser row
            logger.info(f"insert document row: id={id}, source={source}")
            num += 1
            sqlCon.execute(
                """INSERT INTO document (id, source, content_type, file_path, file_size, file_sha256, last_modified)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (id, source, content_type, file_path, file_size, file_sha256, last_modified)
            )
            sqlCon.commit()


        doc.metadata["id"] = id
        doc.metadata["document_id"] = id
        yield doc
    
    # iteration done
    logger.info(f"save_docs_in_sqldb: {num} row(s) inserted - DONE")


# iterate over the document parts, save them in SQL DB, and add IDs
def save_doc_parts_in_vectorstore_and_sqldb(document_id: str, doc_parts_list: Iterator[Document]) -> Iterator[Document]:
    # TODO: split (maybe slit doc by dock in a yield loop to remain reative)
        # save doc with all chunks in SQL DB
        # save all chunks in SQL DB
        # save all chunks in vectorstore

    logger.info(f"save_doc_parts_in_vectorstore_and_sqldb (document_id={document_id}) ...")

    # insert (document) parts
    sqlCon = get_sql_database_connection_after_setup()
    parts_num = 0
    document_parts_num = 0
    for doc_part in doc_parts_list:
        logger.info(f"save_doc_parts_in_vectorstore_and_sqldb: doc_part.metadata={str_limit(doc_part.metadata, 1024)} doc_part.page_content={str_limit(doc_part.page_content)}")

        # extract anker from metadata
        anker = None
        if "anker" in doc_part.metadata:
            anker = doc_part.metadata['anker']
        elif "page_number" in doc_part.metadata:
            anker = f"page {doc_part.metadata['page_number']}"
        elif "start_index" in doc_part.metadata:
            anker = doc_part.metadata['start_index']

        #
        # save part in vectorstore and SQL DB (if not already there)
        #
        part_sha256 = save_single_part_of_single_document_in_vectorstore_and_sqldb(doc_part)

        #
        # insert "document_part" - OR NOT (TODO: check if part already exists)
        #
        if False:
            # update row
            logger.info("UPDATE document_part SET url = ?, file_path = ?, content = ?, last_modified = ? WHERE id = ?", (doc.metadata.url, doc.page_content, doc.metadata))
        # else
        else:
            # insert row
            logger.info(f"insert document_part row: document_id={document_id}, part_sha256={part_sha256}, anker={anker}")
            document_parts_num += 1
            sqlCon.execute(
                """INSERT INTO document_part (document_id, part_sha256, anker)
                                      VALUES (?, ?, ?)""",
                (document_id, part_sha256, anker)
            )
            sqlCon.commit()

        doc_part.metadata["document_id"] = document_id
        doc_part.metadata["part_sha256"] = part_sha256
        yield doc_part
    
    # iteration done
    logger.info(f"save_doc_parts_in_vectorstore_and_sqldb: {document_parts_num} document_part row(s) and {parts_num} part row(s) inserted - DONE")


#
# processing a single part of a single document
#

def save_single_part_of_single_document_in_vectorstore_and_sqldb(doc_part: Document) -> Optional[str]:
    """
    Add a single part of a single document to the SQL DB and the vectorstore.

    If the part is already in the SQL DB/vectorstore, nothing will be done.

    Returns: the ID of the part (=sha256 hash), or None in the case of an error
    """

    # preparation
    part_sha256 = doc_part.metadata["part_sha256"]
    part_content = doc_part.page_content

    try:
        # check if part is already in vectorStore
        vectorStore = get_vectorstore()
        elems_in_vectorstore_result: GetResult = vectorStore.get(ids=[part_sha256])
        logger.debug(f"elem_in_vectorstore={elems_in_vectorstore_result}")
        is_part_in_vectorstore = len(elems_in_vectorstore_result["ids"]) > 0 # this does not work

        # check if part is already in SQL DB
        sqlCon = get_sql_database_connection_after_setup()
        cur = sqlCon.cursor()
        cur.execute("SELECT * FROM part WHERE sha256=?", (part_sha256,))
        num_of_rows = len(cur.fetchall())
        cur.close()
        is_part_in_sql_db = (num_of_rows > 0)

        # save doc_part in vectorstore, if not already there
        # (use is_part_in_sql_db as workaround for is_part_in_vectorstore)
        if not(is_part_in_vectorstore or is_part_in_sql_db):
            logger.debug(f"Add part with id=part_sha256={part_sha256} to vectorStore")
            resultIds = vectorStore.add_texts(texts=[part_content], metadatas=[doc_part.metadata], ids=part_sha256)
            logger.info(f"Added part(s) with id(s)={resultIds} to vectorStore")
        else:
            logger.info(f"Part with id=part_sha256={part_sha256} already in vectorStore")

        # save doc_part in SQL DB, if not already there
        if not(is_part_in_sql_db):
            # save part in SQL DB
            logger.debug(f"insert part row: sha256={part_sha256}, content={str_limit(part_content)} into SQL DB")
            sqlCon.execute(
                """INSERT INTO part (sha256, content)
                             VALUES (?, ?)""",
                (part_sha256, part_content)
            )
            sqlCon.commit()
            logger.info(f"inserted part row: sha256={part_sha256} into SQL DB")
        else:
            # part already in SQL DB
            logger.info(f"Part with sha256={part_sha256} already in SQL DB")

        # done
        return part_sha256

    except Exception as e:
        logger.warning(f"save_single_part_of_single_document_in_vectorstore_and_sqldb(part_sha256={part_sha256}, part={str_limit(doc_part)}): {e}")
        return None
    
    logger.info("END func")

#
# database debugging functions
#

def get_all_docs_from_sqldb() -> List[Dict[str, Any]]:
    # get all rows
    sqlCon = get_sql_database_connection_after_setup()
    cur = sqlCon.cursor()
    cur.execute("SELECT id, source, content_type, file_path, file_size, file_sha256, last_modified FROM document")
    rows = cur.fetchall()
    cur.close()

    # map rows to document dictionaries
    document_dicts = [{
        "id": row[0],
        "source": row[1],
        "content_type": row[2],
        "file_path": row[3],
        "file_size": row[4],
        "file_sha256": row[5],
        "last_modified": row[6],
    } for row in rows]

    return document_dicts

def print_all_docs_from_sqldb():
    docs = get_all_docs_from_sqldb()
    docs_values = [list(doc.values()) for doc in docs]

    for row in docs_values:
        logger.info("  DB row: "+str_limit(row, 200))
    logger.info("All Documents in SQL DB - DONE")


def get_all_doc_parts_from_sqldb() -> List[Dict[str, Any]]:
    # get all rows
    sqlCon = get_sql_database_connection_after_setup()
    cur = sqlCon.cursor()
    cur.execute("SELECT document_id, part_sha256, anker FROM document_part")
    rows = cur.fetchall()
    cur.close()

    # map rows to document_part dictionaries
    document_part_dicts = [{
        "document_id": row[0],
        "part_sha256": row[1],
        "anker": row[2],
    } for row in rows]

    return document_part_dicts

def print_all_doc_parts_from_sqldb():
    doc_parts = get_all_doc_parts_from_sqldb()
    doc_parts_values = [list(doc_part.values()) for doc_part in doc_parts]

    for row in doc_parts_values:
        logger.info("  DB row: "+str_limit(row, 200))
    logger.info("All Document Parts in SQL DB - DONE")

def get_all_parts_from_sqldb() -> List[Dict[str, Any]]:
    # get all rows
    sqlCon = get_sql_database_connection_after_setup()
    cur = sqlCon.cursor()
    cur.execute("SELECT sha256, content FROM part")
    rows = cur.fetchall()
    cur.close()

    # map rows to part dictionaries
    part_dicts = [{
        "sha256": row[0],
        "content": row[1],
    } for row in rows]

    return part_dicts

def print_all_parts_from_sqldb():
    parts = get_all_parts_from_sqldb()
    parts_values = [list(part.values()) for part in parts]

    for row in parts_values:
        logger.info("  DB row: "+str_limit(row, 200))
    logger.info("All Parts in SQL DB - DONE")

def printall():
    logger.info("== printall()")

    global vectorStore
    logger.info(f"vectorStore = {vectorStore}")

    print_all_docs_from_sqldb()
    print_all_parts_from_sqldb()
    print_all_doc_parts_from_sqldb()



#
# basic database functions
#

def get_sql_database_connection_after_setup() -> DBAPIConnection:
    """
    Get the SQL database connection, setup the tables if necessary.

    Returns: the SQL database connection
    """

    global sqlCon
    if sqlCon is None:
        sqlCon = get_sql_database_connection()
            # see also:
            # - https://docs.python.org/3/library/sqlite3.html#sqlite3.threadsafety
            # - https://discuss.python.org/t/is-sqlite3-threadsafety-the-same-thing-as-sqlite3-threadsafe-from-the-c-library/11463

        # setup tables if necessary
        sqlCon.execute(DB_TABLE_document)
        sqlCon.execute(DB_TABLE_part)
        sqlCon.execute(DB_TABLE_document_part)

    return sqlCon
