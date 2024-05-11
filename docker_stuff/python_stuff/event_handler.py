import json
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import psycopg
import redis
import uvicorn

from datadog import initialize, statsd
from ddtrace import tracer
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from event_classes import Event, Subscription
from psycopg_pool import AsyncConnectionPool


load_dotenv()

options = {"statsd_host": "172.28.0.5", "statsd_port": 8125}
initialize(**options)

redis_client = redis.Redis(host="172.28.0.6", port=6379)

tracer.configure(hostname="172.28.0.5", port=8126)
redis_client: redis.Redis = redis.Redis(host="172.28.0.6", port=6379)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
log_file = "./logs/event_handler.log"
handler = RotatingFileHandler(log_file, maxBytes=1000000, backupCount=5)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)


def get_conn_str() -> str:
    return f"""
    dbname={os.getenv('PGDATABASE')}
    user={os.getenv('PGUSER')}
    password={os.getenv('PGPASSWORD')}
    host={os.getenv('PGHOST')}
    port={os.getenv('PGPORT')}
    """


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.async_pool = AsyncConnectionPool(conninfo=get_conn_str())
    yield
    await app.async_pool.close()


app = FastAPI(lifespan=lifespan)


def initialize_db() -> None:
    """
    Initialize the database by creating the necessary table if it doesn't exist,
    and creating indexes on the pubkey and kind columns.

    """
    try:
        conn = psycopg.connect(get_conn_str())
        with conn.cursor() as cur:
            # Create events table if it doesn't already exist
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id VARCHAR(255) PRIMARY KEY,
                    pubkey VARCHAR(255),
                    kind INTEGER,
                    created_at INTEGER,
                    tags JSONB,
                    content TEXT,
                    sig VARCHAR(255)
                );
            """
            )

            index_columns = ["pubkey", "kind"]
            for column in index_columns:
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{str(column)}
                    ON events ({str(column)});
                """
                )

            conn.commit()
        logger.info("Database initialization complete.")
    except psycopg.Error as caught_error:
        logger.info(f"Error occurred during database initialization: {caught_error}")


@app.post("/new_event")
async def handle_new_event(request: Request) -> JSONResponse:
    event_dict = await request.json()
    event_obj = Event(
        event_id=event_dict["id"],
        pubkey=event_dict["pubkey"],
        kind=event_dict["kind"],
        created_at=event_dict["created_at"],
        tags=event_dict["tags"],
        content=event_dict["content"],
        sig=event_dict["sig"],
    )
    logger.debug(f"New event loop iter, ev object is {event_obj.event_id} and {event_obj.kind}")

    try:
        async with request.app.async_pool.connection() as conn:
            async with conn.cursor() as cur:
                if event_obj.kind in [0, 3]:
                    await event_obj.delete_check(conn, cur, statsd)
                    logger.debug(f"Adding event id: {event_obj.event_id}")
                    await event_obj.add_event(conn, cur)
                    logger.debug(f"after query sucess  is: {event_obj.event_id}")
                    return event_obj.evt_response(
                    results_status="true", http_status_code=200
                     )
                elif event_obj.kind == 5:
                    if event_obj.verify_signature(logger):
                        events_to_delete = event_obj.parse_kind5(statsd)
                        await event_obj.delete_event(
                            conn, cur, events_to_delete, logger
                        )
                        return event_obj.evt_response(
                            results_status="true", http_status_code=200
                        )
                    else:
                        return event_obj.evt_response(
                            results_status="flase", http_status_code=200
                        )

                else:
                    try:
                        logger.debug(f"Adding event id: {event_obj.event_id}")
                        await event_obj.add_event(conn, cur)
                        logger.debug(f"after query sucess  is: {event_obj.event_id}")
                        statsd.increment(
                            "nostr.event.added.count", tags=["func:new_event"]
                        )
                    except psycopg.IntegrityError:
                        conn.rollback()
                        logger.info(
                            f"Event with ID {event_obj.event_id} already exists"
                        )
                        return event_obj.evt_response(
                            results_status="false",
                            http_status_code=409,
                            message="duplicate: already have this event",
                        )
                    except Exception as exc:
                        logger.error(f"Exception adding event {exc}")
                        return event_obj.evt_response(
                        results_status="false",
                        http_status_code=400,
                        message="error: failed to add event",
                        )

                return event_obj.evt_response(
                    results_status="true", http_status_code=200
                )

    except Exception:
        logger.debug("Entering gen exc")
        conn.rollback()
        return event_obj.evt_response(
            results_status="false",
            http_status_code=500,
            message="error: could not connect to the database",
        )


@app.post("/subscription")
async def handle_subscription(request: Request) -> JSONResponse:
    try:
        request_payload = await request.json()
        subscription_obj = Subscription(request_payload)

        if not subscription_obj.filters:
            return subscription_obj.sub_response_builder(
                "EOSE", subscription_obj.subscription_id, "", 204
            )

        logger.debug(f"Fiters are: {subscription_obj.filters}")
        logger.debug(f"raw line1 {subscription_obj.filters_raw}")
        (
            tag_values,
            query_parts,
            limit,
            global_search,
        ) = await subscription_obj.parse_filters(subscription_obj.filters, logger)

        sql_query = subscription_obj.base_query_builder(
            tag_values, query_parts, limit, global_search, logger
        )
        
        logger.debug(f"raw filters is {subscription_obj.filters_raw}")
        cached_results = subscription_obj.fetch_data_from_cache(
            str(subscription_obj.filters_raw), redis_client
            
        )
        logger.debug(f"Cached results are {cached_results}")

        if cached_results is None:
            async with app.async_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query=sql_query)
                    listed = await cur.fetchall()
                    if listed:
                        parsed_results = await subscription_obj.query_result_parser(
                            listed
                        )
                        serialized_events = json.dumps(parsed_results)
                        redis_client.setex(
                            str(subscription_obj.filters_raw), 240, serialized_events
                        )
                        logger.debug(f"Caching results , keys: {str(subscription_obj.filters_raw)}   value is : {serialized_events}")
                        return_response = subscription_obj.sub_response_builder(
                            "EVENT",
                            subscription_obj.subscription_id,
                            serialized_events,
                            200,
                        )
                        return return_response

                    else:
                        redis_client.setex(str(subscription_obj.filters_raw), 240, "")
                        return subscription_obj.sub_response_builder(
                            "EOSE", subscription_obj.subscription_id, "", 200
                        )

        elif cached_results:
            event_type = "EVENT"
            try:
                parse_var = json.loads(cached_results.decode("utf-8"))
                results_json = json.dumps(parse_var)
            except:
                logger.warning("Empty cache results, sending EOSE")
            if not parse_var:
                event_type = "EOSE"
                results_json = ""
            return subscription_obj.sub_response_builder(
                event_type, subscription_obj.subscription_id, results_json, 200
            )

        else:
            return subscription_obj.sub_response_builder(
                "EOSE", subscription_obj.subscription_id, "", 200
            )

    except psycopg.Error as exc:
        logger.error(f"Error occurred: {str(exc)}", exc_info=True)
        return subscription_obj.sub_response_builder(
            "EOSE", subscription_obj.subscription_id, "", 500
        )

    except Exception as exc:
        logger.error(f"General exception occurred: {exc}", exc_info=True)
        return subscription_obj.sub_response_builder(
            "EOSE", subscription_obj.subscription_id, "", 500
        )


if __name__ == "__main__":
    initialize_db()
    uvicorn.run(app, host="0.0.0.0", port=80)
