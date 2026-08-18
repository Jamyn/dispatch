import traceback
import logging
from importlib.metadata import entry_points
from sqlalchemy.exc import SQLAlchemyError

from dispatch.plugins.base import plugins, register

logger = logging.getLogger(__name__)


# Plugin endpoints should determine authentication # TODO allow them to specify (kglisson)
def install_plugin_events(api):
    """Adds plugin endpoints to the event router."""
    for plugin in plugins.all():
        if plugin.events:
            api.include_router(plugin.events, prefix="/{organization}/events", tags=["events"])


# Entry points targeting this package are the plugins Dispatch itself ships, so
# an ImportError from one is a packaging bug -- typically a dependency missing
# from requirements-lock.txt, which docker/Dockerfile installs with --no-deps.
# An operator cannot cause or fix it, which is why it is fatal below while a
# third-party plugin's failure is not.
FIRST_PARTY_PLUGIN_PREFIX = "dispatch.plugins."


def install_plugins():
    """Installs plugins associated with dispatch.

    Raises RuntimeError if a plugin Dispatch ships cannot be imported. Every
    other failure is logged and skipped, as before: serving without one
    provider integration is a degraded deployment, but serving without a
    plugin that is supposed to be in the wheel is a broken build, and it is
    otherwise invisible -- the app boots and answers every route regardless.
    """
    dispatch_plugins = entry_points().select(group="dispatch.plugins")

    missing = []
    for ep in dispatch_plugins:
        logger.info(f"Attempting to load plugin: {ep.name}")
        try:
            plugin = ep.load()
            register(plugin)
            logger.info(f"Successfully loaded plugin: {ep.name}")
        except SQLAlchemyError:
            logger.error(
                "Something went wrong with creating plugin rows, is the database setup correctly?"
            )
            logger.error(f"Failed to load plugin {ep.name}:{traceback.format_exc()}")
        except KeyError as e:
            logger.info(f"Failed to load plugin {ep.name} due to missing configuration items. {e}")
        except ImportError:
            # Collected rather than raised here so one run reports every broken
            # plugin; fixing them one restart at a time is the slow way round.
            if ep.value.startswith(FIRST_PARTY_PLUGIN_PREFIX):
                missing.append(ep.name)
            logger.error(f"Failed to load plugin {ep.name}:{traceback.format_exc()}")
        except Exception:
            logger.error(f"Failed to load plugin {ep.name}:{traceback.format_exc()}")

    if missing:
        raise RuntimeError(
            "Dispatch ships these plugins but they could not be imported: "
            f"{', '.join(sorted(missing))}. This is a packaging bug, not a "
            "configuration problem -- most often a dependency absent from "
            "requirements-lock.txt. Refusing to start rather than run without them."
        )
