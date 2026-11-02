#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Trip planner: a LangGraph moderator and three specialist agents, traced with the AGNTCY Observe SDK.

A traveller asks a question about three fictional cities (Celestia, Verdantia and Luminos). The moderator
reads it and hands it to the specialist that can answer, and writes the final answer from the reply:

    itinerary_agent    routes between the cities and their travel times      (find_routes)
    activities_agent   what to see and do in each city                       (read_city_guide)
    concierge_agent    train fares and discounts                             (lookup_fares)

Each question is one SCOPE session. The model is any OpenAI-compatible one:

    export OPENAI_API_KEY=...            # and OPENAI_BASE_URL for a gateway
    uv run trip_planner.py "What is the fastest train route from Celestia to Luminos?"
    uv run trip_planner.py --questions all     # one session per question of questions.py
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Literal, TypedDict

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph, tool as observed_tool
from ioa_observe.sdk.tracing import session_start
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command

APPLICATION_ID = "trip-planner"
DATA = Path(__file__).resolve().parent / "data"
NETWORK = json.loads((DATA / "network.json").read_text(encoding="utf-8"))
CITIES = NETWORK["cities"]

# ---------------------------------------------------------------------------------------------
# Tools: what the specialists can look up
# ---------------------------------------------------------------------------------------------


class UnknownCity(ValueError):
    pass


def _city(name: str) -> str:
    for city in CITIES:
        if city.lower() == name.strip().lower():
            return city
    raise UnknownCity(f"Unknown city {name!r}. The cities are {', '.join(CITIES)}.")


def _error(exc: Exception) -> str:
    """What a tool answers when it cannot help: the model reads it and decides what to do."""
    return json.dumps({"error": str(exc)})


def _neighbours(city: str) -> list[tuple[str, int]]:
    """The cities one train away from ``city``, with the travel time in hours."""
    return [
        (r["to"] if r["from"] == city else r["from"], r["hours"])
        for r in NETWORK["routes"]
        if city in (r["from"], r["to"])
    ]


@tool
@observed_tool(
    name="find_routes",
    description="Lists the train routes between two cities with their travel time",
    application_id=APPLICATION_ID,
)
def find_routes(origin: str, destination: str) -> str:
    """Every train route from ``origin`` to ``destination`` with its travel time in hours, fastest first."""
    try:
        start, end = _city(origin), _city(destination)
    except UnknownCity as exc:
        return _error(exc)
    routes: list[dict] = []

    def walk(city: str, path: list[str], hours: int) -> None:
        if city == end:
            routes.append({"path": path, "hours": hours})
            return
        for neighbour, leg in _neighbours(city):
            if neighbour not in path:
                walk(neighbour, [*path, neighbour], hours + leg)

    walk(start, [start], 0)
    routes.sort(key=lambda r: (r["hours"], len(r["path"])))
    return json.dumps({"origin": start, "destination": end, "routes": routes})


@tool
@observed_tool(
    name="read_city_guide",
    description="Returns the guide of a city: attractions and a suggested day",
    application_id=APPLICATION_ID,
)
def read_city_guide(city: str) -> str:
    """The guide of one city: its attractions with opening hours and prices, and a suggested day."""
    try:
        return (DATA / "guides" / f"{_city(city)}.txt").read_text(encoding="utf-8")
    except UnknownCity as exc:
        return _error(exc)


@tool
@observed_tool(
    name="lookup_fares",
    description="Returns the train fare between two cities and the discounts",
    application_id=APPLICATION_ID,
)
def lookup_fares(origin: str, destination: str) -> str:
    """The one-way and round-trip fare of the train between two cities, and the discounts and passes."""
    try:
        start, end = _city(origin), _city(destination)
    except UnknownCity as exc:
        return _error(exc)
    for r in NETWORK["routes"]:
        if {r["from"], r["to"]} == {start, end}:
            rules = NETWORK["fare_rules"]
            round_trip = round(
                2 * r["fare"] * (1 - rules["round_trip_discount_percent"] / 100), 2
            )
            return json.dumps(
                {
                    "origin": start,
                    "destination": end,
                    "one_way": r["fare"],
                    "round_trip": round_trip,
                    "rules": rules,
                }
            )
    return json.dumps({"error": f"No train runs directly between {start} and {end}."})


# ---------------------------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------------------------

llm = ChatOpenAI(
    model=os.getenv("MODEL", "gpt-4o"),
    temperature=0,
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

SPECIALISTS = {
    "itinerary_agent": (
        [find_routes],
        "Plans train routes between the cities and their travel times",
        "You are an itinerary specialist. Use your tool to find the routes between two cities, and answer with "
        "the fastest route first and its travel time in hours, then the alternatives.",
    ),
    "activities_agent": (
        [read_city_guide],
        "Recommends what to see and do in each city",
        "You are a local guide. Use your tool to read the guide of the city, and recommend its attractions with "
        "their opening hours and prices, in the order you would visit them.",
    ),
    "concierge_agent": (
        [lookup_fares],
        "Answers questions about train fares and discounts",
        "You are a ticketing concierge. Use your tool to find the fare, and answer with the price in dollars, "
        "then the discounts that apply to the traveller.",
    ),
}

MODERATOR_PROMPT = (
    "You are the moderator of a trip planning team. The specialists are:\n"
    + "\n".join(
        f"- {name}: {description}" for name, (_, description, _) in SPECIALISTS.items()
    )
    + "\n\nRead the conversation and decide which specialist should act next. Route to the one the question "
    "needs, and only to the others if the question also needs them. Route to each specialist at most once. Once "
    "a reply answers the question, do not route again: write the complete answer for the traveller in "
    "`final_answer`, from the reply."
)


class Router(TypedDict):
    """The moderator's decision."""

    next: Literal["itinerary_agent", "activities_agent", "concierge_agent", "FINISH"]
    final_answer: str
    """The complete answer to the traveller. Only filled in with FINISH."""


def _specialist_node(name: str):
    tools, description, prompt = SPECIALISTS[name]
    executor = create_agent(llm, tools=tools, system_prompt=prompt)

    @agent(name=name, description=description, application_id=APPLICATION_ID)
    def run(state: MessagesState) -> dict:
        reply = executor.invoke(state)["messages"][-1].content
        return {"role": "user", "content": reply, "name": name}

    def node(state: MessagesState) -> Command[Literal["moderator"]]:
        return Command(update={"messages": [run(state)]}, goto="moderator")

    return node


@agent(
    name="moderator",
    description="Routes the question to a specialist and writes the final answer",
    application_id=APPLICATION_ID,
)
def decide_next(state: MessagesState) -> Router:
    messages = [{"role": "system", "content": MODERATOR_PROMPT}] + state["messages"]
    return llm.with_structured_output(Router).invoke(messages)


def moderator_node(
    state: MessagesState,
) -> Command[
    Literal["itinerary_agent", "activities_agent", "concierge_agent", "__end__"]
]:
    decision = decide_next(state)
    answered = {getattr(message, "name", None) for message in state["messages"]}
    if (
        decision["next"] == "FINISH" or decision["next"] in answered
    ):  # a specialist is asked once
        content = decision.get("final_answer") or state["messages"][-1].content
        return Command(
            goto=END,
            update={
                "messages": [
                    {"role": "assistant", "content": content, "name": "moderator"}
                ]
            },
        )
    return Command(goto=decision["next"])


@graph(
    name="trip_planner",
    description="Answers questions about trips between Celestia, Verdantia and Luminos: routes, things to do and fares",
    application_id=APPLICATION_ID,
)
def create_trip_planner():
    """Build and compile the graph. Returned as is, so the SDK can record its topology."""
    builder = StateGraph(MessagesState)
    builder.add_edge(START, "moderator")
    builder.add_node("moderator", moderator_node)
    for name in SPECIALISTS:
        builder.add_node(name, _specialist_node(name))
    return builder.compile()


def ask(question: str) -> str:
    """One question, one SCOPE session."""
    with session_start():
        planner = create_trip_planner()
        result = planner.invoke(
            {"messages": [HumanMessage(content=question)]}, {"recursion_limit": 25}
        )
    return result["messages"][-1].content


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask the trip planner a question.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "question", nargs="?", help="the question (omit it with --questions)"
    )
    parser.add_argument(
        "--questions",
        choices=["all", "itinerary", "activities", "fares"],
        help="ask the questions of questions.py: every set, or one",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="ask at most this many of them"
    )
    args = parser.parse_args()

    if args.questions:
        from questions import QUESTIONS

        sets = (
            QUESTIONS
            if args.questions == "all"
            else {args.questions: QUESTIONS[args.questions]}
        )
        questions = [
            q for group in zip(*sets.values()) for q in group
        ]  # interleave the sets
        questions = questions[: args.limit] if args.limit else questions
    elif args.question:
        questions = [args.question]
    else:
        parser.error("give a question, or --questions")
    if not os.getenv("OPENAI_API_KEY"):
        parser.exit(1, "Set OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway).\n")

    Observe.init(
        APPLICATION_ID,
        api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318"),
    )
    for number, question in enumerate(questions, 1):
        print(f"[{number}/{len(questions)}] {question}")
        print(f"    {ask(question)}\n")


if __name__ == "__main__":
    main()
