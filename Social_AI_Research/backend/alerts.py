"""Push notification alert service for Social AI Research (SAIR).

Uses ntfy.sh to deliver instant, high-priority push notifications directly
to iOS and Android phones without requiring third-party accounts, API keys,
or credentials.
"""

import asyncio
import logging
import os
from typing import Callable

import httpx

logger = logging.getLogger("alerts")


def get_ntfy_topic() -> str:
    return os.environ.get("NTFY_TOPIC", "").strip()


async def send_ntfy_notification(
    title: str,
    message: str,
    priority: str = "high",
    tags: str = "warning,rotating_light",
    click_url: str | None = None,
) -> bool:
    """Send a push notification to the configured ntfy topic."""
    topic = get_ntfy_topic()
    if not topic:
        return False

    url = f"https://ntfy.sh/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
            if resp.status_code == 200:
                logger.info("Successfully sent ntfy alert to topic '%s': %s", topic, title)
                return True
            else:
                logger.warning(
                    "ntfy.sh responded with HTTP %d: %s", resp.status_code, resp.text
                )
                return False
    except Exception as e:
        logger.error("Failed to send ntfy push notification: %s", e)
        return False


async def run_mongo_health_monitor(
    check_fn: Callable[[], bool] | None = None,
    interval_seconds: int = 30,
) -> None:
    """Background daemon checking MongoDB connectivity and firing phone alerts.
    
    Alerts once immediately when MongoDB transitions from UP -> DOWN.
    Alerts once when MongoDB transitions back from DOWN -> UP.
    """
    import db

    topic = get_ntfy_topic()
    if not topic:
        logger.info("[Health Monitor] NTFY_TOPIC is not set. Phone push alerts are disabled.")
        return

    logger.info(
        "[Health Monitor] Active. Monitoring MongoDB every %ds (Alerts topic: '%s').",
        interval_seconds,
        topic,
    )

    # Initial state: assume alive until checked
    was_down = False

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            if check_fn is not None:
                is_alive = await check_fn() if asyncio.iscoroutinefunction(check_fn) else check_fn()
            else:
                is_alive = await db.is_connected()

            if not is_alive and not was_down:
                # Transitioned to DOWN
                was_down = True
                logger.warning("[Health Monitor] MongoDB detected DOWN! Sending push notification...")
                await send_ntfy_notification(
                    title="🚨 SAIR Alert: MongoDB is DOWN!",
                    message=(
                        "MongoDB is unreachable on localhost:27017.\n"
                        "The backend is in degraded mode.\n\n"
                        "Server restart command:\nsudo systemctl restart mongod"
                    ),
                    priority="urgent",
                    tags="skull,warning,rotating_light",
                )
            elif is_alive and was_down:
                # Transitioned to RECOVERED
                was_down = False
                logger.info("[Health Monitor] MongoDB RECOVERED! Sending recovery push notification...")
                await send_ntfy_notification(
                    title="✅ SAIR Notice: MongoDB Restored",
                    message="MongoDB connection has been restored and is responding normally.",
                    priority="default",
                    tags="white_check_mark,tada",
                )
        except asyncio.CancelledError:
            logger.info("[Health Monitor] Stopped.")
            break
        except Exception as e:
            logger.error("[Health Monitor] Unexpected error in monitor loop: %s", e)
            await asyncio.sleep(5)

