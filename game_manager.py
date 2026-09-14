import random
import time
from questions import QUESTIONS, QUESTIONS_BY_CATEGORY

internal_games = {}
tournaments = {}
private_sessions = {}
matchmaking_pool = []
_match_counter = [0]


def new_match_id():
    _match_counter[0] += 1
    return _match_counter[0]


def get_question(category=None):
    if category and category in QUESTIONS_BY_CATEGORY:
        return random.choice(QUESTIONS_BY_CATEGORY[category])
    return random.choice(QUESTIONS)


def initial_lives(team_size):
    if team_size <= 3:
        return 3
    return team_size


class InternalGame:
    def __init__(self, chat_id, chat_name, team_size):
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.category = None
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
        self.first_bidder_team = random.choice([1, 2])
        self.forced = False
        self.round_stats = []
        self.start_time = None
        self.end_time = None
        self.chat_name_display = ""
        self.opponent_resolved = False
        self.answer_watcher_task = None
        self.answer_start_time = None
        self._join_timeout_task = None

    def split_teams(self):
        shuffled = self.players[:]
        random.shuffle(shuffled)
        self.team1 = shuffled[:self.team_size]
        self.team2 = shuffled[self.team_size:self.team_size * 2]

    def swap_first_bidder(self):
        self.first_bidder_team = 2 if self.first_bidder_team == 1 else 1

    def record_round(self, round_num, bidder_id, bidder_team, bid, success, answers_count, duration, forced):
        self.round_stats.append({
            "round": round_num,
            "bidder_id": bidder_id,
            "bidder_team": bidder_team,
            "bid": bid,
            "success": success,
            "answers_count": answers_count,
            "duration": duration,
            "forced": forced,
        })


class Tournament:
    def __init__(self, group1_id, group1_name, group2_id, group2_name, squad=5):
        self.match_id = new_match_id()
        self.group1_id = group1_id
        self.group1_name = group1_name
        self.group2_id = group2_id
        self.group2_name = group2_name
        self.squad = squad
        self.category = None
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
        self.first_bidder_team = random.choice([1, 2])
        self.forced = False
        self.round_stats = []
        self.start_time = None
        self.end_time = None
        self.nom_msg_ids = {group1_id: None, group2_id: None}
        self.opponent_resolved = False
        self.answer_watcher_task = None
        self.answer_start_time = None

    def resolve_top(self, gid):
        lst = []
        for uid, d in self.candidates.get(gid, {}).items():
            lst.append({"user_id": uid, "name": d["name"], "votes": len(d["votes"]), "ts": d["ts"]})
        lst.sort(key=lambda x: (-x["votes"], x["ts"]))
        return lst[:self.squad]

    def both_groups(self):
        return [self.group1_id, self.group2_id]

    def swap_first_bidder(self):
        self.first_bidder_team = 2 if self.first_bidder_team == 1 else 1

    def record_round(self, round_num, bidder_id, bidder_team, bid, success, answers_count, duration, forced):
        self.round_stats.append({
            "round": round_num,
            "bidder_id": bidder_id,
            "bidder_team": bidder_team,
            "bid": bid,
            "success": success,
            "answers_count": answers_count,
            "duration": duration,
            "forced": forced,
        })


# ============ لعبة 1 ضد 1 (طلب ب) ============

class DuelGame:
    """
    لعبة 1 ضد 1 مخصصة عندما يقوم شخص بالرد على شخص آخر بأمر /1v1.
    - team_size = 1 لكل فريق.
    - لاعب واحد فقط في كل فريق.
    """
    def __init__(self, chat_id, chat_name, player1_id, player1_name, player2_id, player2_name):
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.chat_name_display = chat_name
        self.team_size = 1
        self.required_total = 2
        self.players = [player1_id, player2_id]
        self.names = [player1_name, player2_name]
        self.team1 = [player1_id]
        self.team2 = [player2_id]
        self.lives = 3
        self.team1_points = 3
        self.team2_points = 3
        self.round = 1
        self.state = "playing"
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
        self._name_cache = {player1_id: player1_name, player2_id: player2_name}
        self.team1_label = player1_name
        self.team2_label = player2_name
        self.team1_played = []
        self.team2_played = []
        self.first_bidder_team = random.choice([1, 2])
        self.forced = False
        self.round_stats = []
        self.start_time = time.time()
        self.end_time = None
        self.opponent_resolved = False
        self.answer_watcher_task = None
        self.answer_start_time = None

    def split_teams(self):
        pass

    def swap_first_bidder(self):
        self.first_bidder_team = 2 if self.first_bidder_team == 1 else 1

    def record_round(self, round_num, bidder_id, bidder_team, bid, success, answers_count, duration, forced):
        self.round_stats.append({
            "round": round_num,
            "bidder_id": bidder_id,
            "bidder_team": bidder_team,
            "bid": bid,
            "success": success,
            "answers_count": answers_count,
            "duration": duration,
            "forced": forced,
        })


# ============ لعبة ضد الذكاء الاصطناعي (طلب و) ============

class AIGame:
    """
    لعبة تفاعلية ضد الذكاء الاصطناعي في الخاص.
    تشبه اللعب ضد إنسان حقيقي.
    """
    def __init__(self, user_id, difficulty="medium"):
        self.user_id = user_id
        self.difficulty = difficulty
        self.player_points = 100
        self.ai_points = 100
        self.round = 1
        self.state = "idle"
        self.question = None
        self.player_bid = 0
        self.ai_bid = 0
        self.player_answers = []
        self.ai_answers = []
        self.expected_count = 0
        self.turn = "player"  # player or ai
        self.tracked_messages = []
        self.round_stats = []
        self.pending_ai_decision = None  # accept / raise / force

    def record_round(self, bidder, bid, success, answers_count, duration=0, forced=False):
        self.round_stats.append({
            "round": self.round,
            "bidder": bidder,
            "bid": bid,
            "success": success,
            "answers_count": answers_count,
            "duration": duration,
            "forced": forced,
        })
