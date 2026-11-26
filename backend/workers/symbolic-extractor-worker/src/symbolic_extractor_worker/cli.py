# Load .env BEFORE any other imports so that libraries reading os.environ at import time see it.
import os

from dotenv import load_dotenv

_here = os.path.dirname(os.path.abspath(__file__))
# symbolic-extractor-worker/.env (src/symbolic_extractor_worker/../../.env)
# Variables set by the runtime (docker --env-file/-e) keep precedence.
load_dotenv(dotenv_path=os.path.abspath(os.path.join(_here, "..", "..", ".env")), override=False)

import asyncio  # noqa: E402
import logging  # noqa: E402
from typing import Optional  # noqa: E402

import click  # noqa: E402
from worker_base.base_worker import BaseWorker  # noqa: E402
from worker_base.utils import (  # noqa: E402
    configure_worker_logging,
    mask_url_password,
    resolve_max_inflight_messages,
    resolve_rabbitmq_url,
)

from symbolic_extractor_worker import queues, worker  # noqa: E402

logger = logging.getLogger(__name__)


def _build_worker(
    rabbitmq_url: str,
    input_queue: str,
    output_queue: Optional[list[str]],
    feedback_queue: Optional[str],
    max_sessions: int,
    max_inflight_messages: int,
    same_claims: bool,
    debug: bool,
) -> BaseWorker:
    return worker.SymbolicExtractorWorker(
        rabbitmq_url=rabbitmq_url,
        input_queue=input_queue,
        output_queue=output_queue or [],
        feedback_queue=feedback_queue,
        message_limit=max_sessions,
        max_inflight_messages=max_inflight_messages,
        same_claims=same_claims,
        debug=debug,
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--rabbitmq-url", type=str, default=None, help="RabbitMQ URL (default: RABBITMQ_URL)")
@click.option(
    "--input-queue",
    type=str,
    default=None,
    help=f"Input queue (default: {queues.NEW_SESSION_TO_SYMBOLIC_EXTRACTOR_Q}, "
    "or SYMBOLIC_EXTRACTOR_INPUT_QUEUE)",
)
@click.option(
    "--output-queue",
    type=str,
    multiple=True,
    default=None,
    help=f"Output queues (default: {queues.NEW_SESSION_TO_FAILURE_DETECTION_Q}, or "
    "SYMBOLIC_EXTRACTOR_OUTPUT_QUEUE, comma-separated)",
)
@click.option(
    "--feedback-queue",
    type=str,
    default=None,
    help="Feedback queue (optional, SYMBOLIC_EXTRACTOR_FEEDBACK_QUEUE)",
)
@click.option("--max-sessions", type=int, default=-1, help="Sessions to process (-1: unlimited)")
@click.option(
    "--max-inflight-messages",
    type=int,
    default=2,
    help="Sessions processed concurrently in one process (-1: auto CPU)",
)
@click.option(
    "--no-same-claims",
    is_flag=True,
    default=False,
    help="Skip the embedding comparison that adds same_claims (or SYMBOLIC_EXTRACTOR_NO_SAME_CLAIMS=1)",
)
@click.option("--debug", is_flag=True, default=False, help="Detailed logging")
@click.option("--run-once", is_flag=True, default=False, help="Single message mode (Argo)")
@click.option("--input", type=str, default=None, help="Input message file for --run-once")
@click.option("--output", type=str, default=None, help="Output file for --run-once")
@click.option("--test", is_flag=True, default=False, help="Print 'I'm Alive' and exit")
def main(
    rabbitmq_url: Optional[str],
    input_queue: Optional[str],
    output_queue: tuple,
    feedback_queue: Optional[str],
    max_sessions: int,
    max_inflight_messages: int,
    no_same_claims: bool,
    debug: bool,
    run_once: bool,
    input: Optional[str],
    output: Optional[str],
    test: bool,
):
    """Run the symbolic extractor worker: extract the symbolic state of a session into the KG."""
    configure_worker_logging(debug=debug)
    if test:
        print("I'm Alive")
        return

    rabbitmq_url = resolve_rabbitmq_url(rabbitmq_url)
    input_queue = input_queue or os.getenv(
        "SYMBOLIC_EXTRACTOR_INPUT_QUEUE", queues.NEW_SESSION_TO_SYMBOLIC_EXTRACTOR_Q
    )
    output_env = os.getenv("SYMBOLIC_EXTRACTOR_OUTPUT_QUEUE")
    output_queues = list(output_queue) or (
        output_env.split(",") if output_env else [queues.NEW_SESSION_TO_FAILURE_DETECTION_Q]
    )
    feedback_queue = feedback_queue or os.getenv("SYMBOLIC_EXTRACTOR_FEEDBACK_QUEUE")
    inflight = resolve_max_inflight_messages(
        max_inflight_messages, error_subject="Symbolic extractor max inflight messages"
    )

    logger.info("RabbitMQ URL: %s", mask_url_password(rabbitmq_url))
    logger.info("Input queue: %s | output queues: %s", input_queue, output_queues)

    built = _build_worker(
        rabbitmq_url=rabbitmq_url,
        input_queue="argo" if run_once else input_queue,
        output_queue=[] if run_once else output_queues,
        feedback_queue=None if run_once else feedback_queue,
        max_sessions=1 if run_once else max_sessions,
        max_inflight_messages=1 if run_once else inflight,
        same_claims=not (no_same_claims or os.getenv("SYMBOLIC_EXTRACTOR_NO_SAME_CLAIMS")),
        debug=debug,
    )
    try:
        if run_once:
            if not input or not output:
                raise ValueError("--run-once mode requires both --input and --output file paths")
            asyncio.run(built.run_argo_message(input, output))
        else:
            asyncio.run(built.run())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except Exception as exc:
        logger.exception("Worker failed with error: %s", exc)
        raise


if __name__ == "__main__":
    main()
