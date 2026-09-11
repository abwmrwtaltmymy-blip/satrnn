import random
import asyncio
import time
from questions import QUESTIONS
from database import add_points, update_win_loss
from utils import evaluate_answers_with_ai

internal_games = {}
tournaments = {}
private_sessions = {}
matchmaking_pool = []
_match_counter = [0]

def new_match_id():
    _match_counter[0] += 1
    return _match_counter[0]

def get_question():
    return random.choice(QUESTIONS)

def initial_lives(team_size):
    if team_size <= 2:
        return 3
    return team_size

class InternalGame:
    def __init__(self, chat_id, chat_name, team_size):
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.team_size = team_size
        self.required_total = team_size * 2
        self.players = []
        self.names = []
        self.team1 = []
        self.team2 = []
        self.lives = initial_lives(team_size)
        self.team1_points = self.lives
        self.team2_points = self.lives
        self.round = 1
        self.state = "waiting"
        self.question = None
        self.bidder = None
        self.opponent = None
        self.current_bid = 0
        self.answers = []
        self.ready = set()
        self.bidding_task = None
        self.answer_task = None
        self.opponent_task = None
        self.tracked_messages = []
        self.round_messages = []
        self.pin_msg_id = None
        self.consecutive_timeouts = 0
        self._name_cache = {}
        self.team1_label = ""
        self.team2_label = ""
        self.team1_played = []
        self.team2_played = []
        self.first_bidder_team = 1

    def split_teams(self):
        shuffled = self.players[:]
        random.shuffle(shuffled)
        self.team1 = shuffled[:self.team_size]
        self.team2 = shuffled[self.team_size:self.team_size * 2]

class Tournament:
    def __init__(self, group1_id, group1_name, group2_id, group2_name, squad=5):
        self.match_id = new_match_id()
        self.group1_id = group1_id
        self.group1_name = group1_name
        self.group2_id = group2_id
        self.group2_name = group2_name
        self.squad = squad
        self.state = "nominating"
        self.candidates = {group1_id: {}, group2_id: {}}
        self.team1 = []
        self.team2 = []
        self.lives = initial_lives(squad)
        self.team1_points = self.lives
        self.team2_points = self.lives
        self.round = 1
        self.question = None
        self.bidder = None
        self.opponent = None
        self.current_bid = 0
        self.answers = []
        self.ready = set()
        self.bidding_task = None
        self.answer_task = None
        self.opponent_task = None
        self.tracked_messages = []
        self.round_messages = []
        self.pin_msg_id = None
        self.consecutive_timeouts = 0
        self.team1_played = []
        self.team2_played = []
        self.first_bidder_team = 1

    def resolve_top(self, gid):
        lst = []
        for uid, d in self.candidates.get(gid, {}).items():
            lst.append({"user_id": uid, "name": d["name"], "votes": len(d["votes"]), "ts": d["ts"]})
        lst.sort(key=lambda x: (-x["votes"], x["ts"]))
        return lst[:self.squad]

    def both_groups(self):
        return [self.group1_id, self.group2_id]
