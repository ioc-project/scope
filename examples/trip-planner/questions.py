#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Questions for the trip planner, in three sets.

The questions of a set ask for the same thing in slightly different words, so SCOPE's semantic grouping
puts them in one group. The three sets ask for different things (a route and its travel time, what to
do in a city, a ticket price), so they end up in three distinct groups. The wording is close inside a set
on purpose: sessions are grouped by how near the embeddings of their questions are (a cosine distance of
at most 0.15 by default), and looser paraphrases would spread over several groups.

The last question of each set is a trap: it asks about Atlantis, a city the planner does not know. The
question reads like its ten peers and is grouped with them, but the planner cannot answer it. That is the
kind of session that the analysis of a group is meant to single out.
"""

QUESTIONS = {
    # routes and travel times: handled by the itinerary agent
    "itinerary": [
        "What is the fastest route from Celestia to Luminos, and how many hours does the trip take?",
        "What is the fastest route from Verdantia to Celestia, and how many hours does the trip take?",
        "What is the fastest route from Luminos to Verdantia, and how many hours does the trip take?",
        "Which route from Celestia to Verdantia has the shortest travel time in hours?",
        "Which route from Luminos to Celestia has the shortest travel time in hours?",
        "Which route from Verdantia to Luminos has the shortest travel time in hours?",
        "How long is the journey from Celestia to Luminos, and which route is quickest?",
        "How long is the journey from Verdantia to Celestia, and which route is quickest?",
        "How long is the journey from Luminos to Verdantia, and which route is quickest?",
        "Plan my route from Celestia to Verdantia and tell me the travel time in hours.",
        # the trap
        "What is the fastest route from Celestia to Atlantis, and how many hours does the trip take?",
    ],
    # what to see and do: handled by the activities agent
    "activities": [
        "What are the best attractions and things to do in Celestia for a day of sightseeing?",
        "What are the best attractions and things to do in Luminos for a day of sightseeing?",
        "What are the best attractions and things to do in Verdantia for a day of sightseeing?",
        "Which attractions and activities should I do in Celestia during a day of sightseeing?",
        "Which attractions and activities should I do in Luminos during a day of sightseeing?",
        "Which attractions and activities should I do in Verdantia during a day of sightseeing?",
        "What are the top sights to see and activities to do in Celestia on a day trip?",
        "What are the top sights to see and activities to do in Luminos on a day trip?",
        "What are the top sights to see and activities to do in Verdantia on a day trip?",
        "Recommend the best attractions and things to do in Celestia for a day of sightseeing.",
        # the trap
        "What are the best attractions and things to do in Atlantis for a day of sightseeing?",
    ],
    # ticket prices: handled by the concierge agent
    "fares": [
        "How much does a ticket from Celestia to Luminos cost in dollars, and are there discounts?",
        "How much does a ticket from Verdantia to Celestia cost in dollars, and are there discounts?",
        "How much does a ticket from Luminos to Verdantia cost in dollars, and are there discounts?",
        "What is the ticket price in dollars from Celestia to Verdantia, and are there discounts?",
        "What is the ticket price in dollars from Luminos to Celestia, and are there discounts?",
        "What is the ticket price in dollars from Verdantia to Luminos, and are there discounts?",
        "What is the fare in dollars from Celestia to Luminos, one way or round trip, with discounts?",
        "What is the fare in dollars from Verdantia to Celestia, one way or round trip, with discounts?",
        "What is the fare in dollars from Luminos to Verdantia, one way or round trip, with discounts?",
        "Tell me the ticket price in dollars from Celestia to Verdantia, with any discounts.",
        # the trap
        "What is the ticket price in dollars from Celestia to Atlantis, and are there discounts?",
    ],
}
