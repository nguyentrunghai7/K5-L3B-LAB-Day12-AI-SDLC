"""Phase 1 restaurant simulation, following PRD-0 sections D, E and F.

Timers -> ready dishes -> movement/interact -> departures/payment -> arrivals.
Stock includes raw reservations; cooking consumes stock exactly once.
"""

import copy
import math

from ..errors import BadInput
from ..ids import id_key, sort_by_id
from .ramp import ramp_velocity


class QuanSim:
    mode = "quan"
    phase = "quan"

    def __init__(self, tuning, scenario, streams, ids, inventory=None):
        t = tuning["quan"]
        scenario = copy.deepcopy(scenario)
        self.tick_hz = tuning["tick_hz"]
        self.phase_tick = 0
        self.random_customers = scenario.get("random_customers", True)
        self.t = t
        self.menu = tuning["menu"]
        self.rng = streams
        self.ids = ids
        p = scenario.get("player", {})
        self.pos = list(p.get("pos", t["start_pos"]))
        self.vel = [float(c) for c in p.get("vel", [0.0, 0.0])]
        self._tgt = None
        self._ramp_left = 0
        # kho: --inventory HOẶC scenario.stock (cả hai -> cli đã báo mã 2)
        self.stock = dict(inventory if inventory is not None else scenario.get("stock", {}))
        self.hand = scenario.get("hand")
        self.time_left_ticks = scenario.get("time_left_ticks", self._ticks(t["duration_s"]))
        self.reputation = scenario.get("reputation", t["reputation_initial"])
        self.stoves = [dict(s, state=s.get("state", "idle"), dish=s.get("dish"),
                            cook_ticks_left=s.get("cook_ticks_left", 0), ready_ticks=s.get("ready_ticks", 0))
                       for s in self._merge(t["stoves"], scenario.get("stoves", []))]
        self.tables = self._merge(t["tables"], scenario.get("tables", []))
        self.customers = scenario.get("customers", [])
        self._prepare_customers()
        self.money = 0
        self.earned = 0
        self.shift_earned = 0
        self.done = False
        self.reason = None

    @staticmethod
    def _merge(defaults, updates):
        by_id = {d["id"]: dict(d) for d in defaults}
        for u in updates:
            if u["id"] not in by_id:
                from ..errors import BadInput
                raise BadInput(f"id bếp/bàn không tồn tại: {u['id']}")
            by_id[u["id"]].update(u)
        return sort_by_id(list(by_id.values()))

    def snapshot(self) -> dict:
        occupied = {c["table_id"]: c["id"] for c in self.customers}
        return {
            "player": {"pos": self.pos},
            "stock": {k: self.stock[k] for k in sorted(self.stock) if self.stock[k] > 0},
            "hand": self.hand,
            "stoves": [{k: s.get(k) for k in ("id", "pos", "state", "dish", "cook_ticks_left", "ready_ticks")}
                       for s in self.stoves],
            "tables": [{"id": tb["id"], "pos": tb["pos"], "customer_id": occupied.get(tb["id"])} for tb in self.tables],
            "customers": sort_by_id(self.customers),
        }

    def _ticks(self, seconds):
        return int(round(seconds * self.tick_hz))

    def _order_key(self, customer):
        return customer["order_tick"], id_key(customer["table_id"])

    def _prepare_customers(self):
        """Normalize fixture defaults and reject contradictory order ownership."""
        if not isinstance(self.random_customers, bool):
            raise BadInput("random_customers must be boolean")
        if (not isinstance(self.reputation, (int, float)) or isinstance(self.reputation, bool)
                or not math.isfinite(self.reputation)
                or not self.t["reputation_min"] <= self.reputation <= self.t["reputation_max"]):
            raise BadInput("reputation outside tuning limits")
        table_ids = {t["id"] for t in self.tables}
        seen, occupied = set(), set()
        allowed = {"id", "table_id", "dish_id", "order_tick", "state", "patience_ticks_left",
                   "eat_ticks_left", "order_status", "reserved", "base_price", "tip"}
        for c in self.customers:
            if (set(c) - allowed or not isinstance(c.get("dish_id"), str)
                    or c["dish_id"] not in self.menu):
                raise BadInput("invalid customer fields or dish")
            if c["id"] in seen or c.get("table_id") not in table_ids or c["table_id"] in occupied:
                raise BadInput("duplicate customer or invalid/occupied table")
            seen.add(c["id"])
            occupied.add(c["table_id"])
            self.ids.reserve(c["id"])
            c.setdefault("state", "waiting")
            c.setdefault("order_tick", 0)
            c.setdefault("patience_ticks_left", self._ticks(self.t["patience_s"]))
            c.setdefault("eat_ticks_left", 0)
            c.setdefault("order_status", "reserved")
            for key in ("order_tick", "patience_ticks_left", "eat_ticks_left"):
                if type(c[key]) is not int or c[key] < 0:
                    raise BadInput("invalid customer timer")
            if c["state"] not in ("waiting", "eating"):
                raise BadInput("invalid customer state")
            if c["order_status"] not in ("reserved", "assigned", "unfulfillable", "served"):
                raise BadInput("invalid order status")
        reserved = {}
        for c in sorted(self.customers, key=self._order_key):
            recipe = self.menu[c["dish_id"]]["recipe"]
            c.setdefault("reserved", dict(recipe) if c["order_status"] == "reserved" else {})
            if c["reserved"] != (recipe if c["order_status"] == "reserved" else {}):
                raise BadInput("reservation does not match order")
            for kind, amount in c["reserved"].items():
                if type(amount) is not int or amount <= 0:
                    raise BadInput("invalid reservation amount")
                reserved[kind] = reserved.get(kind, 0) + amount
            if c["state"] == "eating":
                if c["order_status"] != "served" or c["eat_ticks_left"] <= 0:
                    raise BadInput("eating customer must have a served order and positive timer")
                if any(type(c.get(k)) is not int or c[k] < (1 if k == "base_price" else 0)
                       for k in ("base_price", "tip")):
                    raise BadInput("eating customer requires base_price and tip")
            elif c["order_status"] == "served" or c["eat_ticks_left"] != 0:
                raise BadInput("waiting customer has eating state")
        if any(amount > self.stock.get(kind, 0) for kind, amount in reserved.items()):
            raise BadInput("reservations exceed physical stock")
        orders = {c["id"]: c for c in self.customers}
        owned = {}
        for stove in self.stoves:
            if stove["state"] not in ("idle", "cooking", "ready"):
                raise BadInput("invalid stove state")
            if any(type(stove[k]) is not int or stove[k] < 0
                   for k in ("cook_ticks_left", "ready_ticks")):
                raise BadInput("invalid stove timer")
            if stove["state"] == "idle":
                if stove["dish"] is not None or stove["cook_ticks_left"] or stove["ready_ticks"]:
                    raise BadInput("idle stove has a dish or timer")
            else:
                if ((stove["state"] == "cooking" and
                     (stove["cook_ticks_left"] <= 0 or stove["ready_ticks"] != 0))
                        or (stove["state"] == "ready" and stove["cook_ticks_left"] != 0)):
                    raise BadInput("stove state contradicts timers")
                self._validate_dish(stove["dish"], orders, owned)
        if self.hand is not None:
            self._validate_dish(self.hand, orders, owned)
        for c in self.customers:
            if (c["order_status"] == "assigned") != (owned.get(c["id"], 0) == 1):
                raise BadInput("assigned order must own exactly one dish")

    @staticmethod
    def _validate_dish(dish, orders, owned):
        if not isinstance(dish, dict) or set(dish) != {"order_id", "dish_id", "base_price"}:
            raise BadInput("invalid dish payload")
        if not isinstance(dish["order_id"], str) or not isinstance(dish["dish_id"], str):
            raise BadInput("dish identifiers must be strings")
        c = orders.get(dish["order_id"])
        if (c is None or c["order_status"] != "assigned" or c["state"] != "waiting"
                or dish["dish_id"] != c["dish_id"]
                or type(dish["base_price"]) is not int or dish["base_price"] <= 0):
            raise BadInput("dish does not match assigned order")
        owned[c["id"]] = owned.get(c["id"], 0) + 1
        if owned[c["id"]] != 1:
            raise BadInput("order owns duplicate dishes")

    def step(self, inp):
        if self.done:
            return []
        events = []
        self.phase_tick += 1
        self.time_left_ticks = max(0, self.time_left_ticks - 1)
        self._update_timers(events)
        self._move(inp)
        if inp.pressed("interact"):
            self._interact(events)
        self._settle_customers(events)
        if self.time_left_ticks == 0:
            self.done = True
            self.reason = "time"
            events.append(("quan_end", {"reason": "time", "money": self.money, "earned": self.earned,
                                        "shift_earned": self.shift_earned, "reputation": self.reputation}))
        else:
            self._arrive(events)
        return events

    def _update_timers(self, events):
        for s in self.stoves:
            if s["state"] == "ready":
                s["ready_ticks"] += 1
            elif s["state"] == "cooking":
                s["cook_ticks_left"] = max(0, s["cook_ticks_left"] - 1)
        for c in self.customers:
            key = "eat_ticks_left" if c["state"] == "eating" else "patience_ticks_left"
            c[key] = max(0, c[key] - 1)
        for s in sort_by_id(self.stoves):
            if s["state"] == "cooking" and s["cook_ticks_left"] == 0:
                s["state"], s["ready_ticks"] = "ready", 0
                events.append(("dish_ready", self._dish_event(s)))

    @staticmethod
    def _dish_event(stove):
        return {"stove_id": stove["id"], "order_id": stove["dish"]["order_id"],
                "dish_id": stove["dish"]["dish_id"]}

    def _interact(self, events):
        candidates = []
        for priority, collection in enumerate((self.stoves, self.tables)):
            for obj in sort_by_id(collection):
                distance = math.dist(self.pos, obj["pos"])
                if distance <= self.t["interact_radius"]:
                    candidates.append((distance, priority, id_key(obj["id"]), obj))
        if not candidates:
            return
        _, priority, _, obj = min(candidates, key=lambda entry: entry[:3])
        if priority == 0:
            self._interact_stove(obj, events)
        else:
            self._interact_table(obj, events)

    def _interact_stove(self, stove, events):
        if stove["state"] == "ready" and self.hand is None:
            events.append(("dish_picked", self._dish_event(stove)))
            self.hand = stove["dish"]
            self._clear_stove(stove)
        elif stove["state"] == "idle":
            orders = [c for c in self.customers if c["order_status"] == "reserved"]
            if not orders:
                return
            c = min(orders, key=self._order_key)
            recipe = c["reserved"]
            for kind, amount in recipe.items():
                self.stock[kind] -= amount
            dish = {"order_id": c["id"], "dish_id": c["dish_id"],
                    "base_price": self.menu[c["dish_id"]]["price"]}
            items = [kind for kind in sorted(recipe) for _ in range(recipe[kind])]
            c["reserved"], c["order_status"] = {}, "assigned"
            stove.update(state="cooking", dish=dish,
                         cook_ticks_left=self._ticks(self.menu[c["dish_id"]]["cook_s"]), ready_ticks=0)
            events.append(("cook_started", dict(self._dish_event(stove), items=items,
                                                base_price=dish["base_price"])))

    def _interact_table(self, table, events):
        if self.hand is None:
            return
        c = next((c for c in self.customers if c["table_id"] == table["id"]), None)
        if c is None or c["state"] != "waiting" or c["id"] != self.hand["order_id"]:
            return
        price = self.hand["base_price"]
        ratio = c["patience_ticks_left"] / self._ticks(self.t["patience_s"])
        tip = math.floor(price * self.t["tip_rate"]) if ratio >= self.t["tip_min_patience_ratio"] else 0
        c.update(state="eating", order_status="served", eat_ticks_left=self._ticks(self.t["eat_s"]),
                 base_price=price, tip=tip)
        self.hand = None
        events.append(("served", {"order_id": c["id"], "table_id": c["table_id"],
                                  "dish_id": c["dish_id"], "patience_ticks_left": c["patience_ticks_left"],
                                  "base_price": price, "tip": tip}))

    @staticmethod
    def _clear_stove(stove):
        stove.update(state="idle", dish=None, cook_ticks_left=0, ready_ticks=0)

    def _change_reputation(self, delta):
        self.reputation = min(self.t["reputation_max"], max(self.t["reputation_min"],
                                                         self.reputation + delta))

    def _settle_customers(self, events):
        for c in sort_by_id(list(self.customers), key=lambda c: c["table_id"]):
            if c["state"] == "eating" and (c["eat_ticks_left"] == 0 or self.time_left_ticks == 0):
                amount = c["base_price"] + c["tip"]
                self.money += amount
                self.earned += amount
                self.shift_earned += amount
                if c["tip"] > 0:
                    self._change_reputation(self.t["tip_reputation"])
                reason = "finished_eating" if c["eat_ticks_left"] == 0 else "closing"
                events.append(("paid", {"order_id": c["id"], "table_id": c["table_id"],
                                        "base_price": c["base_price"], "tip": c["tip"],
                                        "amount": amount, "reason": reason}))
                self._remove_customer(c)
            elif c["state"] == "waiting" and (c["patience_ticks_left"] == 0 or self.time_left_ticks == 0):
                reason = "closing" if self.time_left_ticks == 0 else "patience"
                if reason == "patience":
                    self._change_reputation(self.t["left_reputation_delta"])
                self._remove_customer(c)
                events.append(("customer_left", {"order_id": c["id"], "table_id": c["table_id"],
                                                 "reason": reason, "reputation": self.reputation}))

    def _remove_customer(self, customer):
        customer["reserved"] = {}
        for stove in self.stoves:
            if stove["dish"] is not None and stove["dish"]["order_id"] == customer["id"]:
                self._clear_stove(stove)
        if self.hand is not None and self.hand["order_id"] == customer["id"]:
            self.hand = None
        self.customers.remove(customer)

    def _arrive(self, events):
        first = self.t["first_customer_tick"]
        interval = self._ticks(self.t["customer_interval_s"])
        if not self.random_customers or self.phase_tick < first or (self.phase_tick - first) % interval:
            return
        occupied = {c["table_id"] for c in self.customers}
        table = next((t for t in sort_by_id(self.tables) if t["id"] not in occupied), None)
        if table is None:
            return
        available = dict(self.stock)
        for c in self.customers:
            for kind, amount in c["reserved"].items():
                available[kind] = available.get(kind, 0) - amount
        dishes = [dish for dish in sorted(self.menu)
                  if all(available.get(k, 0) >= n for k, n in self.menu[dish]["recipe"].items())]
        if not dishes:
            events.append(("no_dish", {}))
            return
        dish = self.rng.get("customer_dish").choice(dishes)
        c = {"id": self.ids.new("C"), "table_id": table["id"], "dish_id": dish,
             "order_tick": self.phase_tick, "state": "waiting", "order_status": "reserved",
             "patience_ticks_left": self._ticks(self.t["patience_s"]), "eat_ticks_left": 0,
             "reserved": dict(self.menu[dish]["recipe"])}
        self.customers.append(c)
        events.append(("customer_arrived", {"order_id": c["id"], "table_id": c["table_id"], "dish_id": dish}))

    def _move(self, inp):
        speed = self.t["walk_speed"]
        mx, mz = inp.move
        self.vel = ramp_velocity(self, (mx * speed, mz * speed), int(self.t["ramp_max_ticks"]))
        b = self.t["bounds"]
        for i, axis in enumerate(("x", "z")):
            self.pos[i] += self.vel[i] / self.tick_hz
            lo, hi = b[axis]
            if self.pos[i] <= lo:
                self.pos[i], self.vel[i] = lo, max(0.0, self.vel[i])
            elif self.pos[i] >= hi:
                self.pos[i], self.vel[i] = hi, min(0.0, self.vel[i])
