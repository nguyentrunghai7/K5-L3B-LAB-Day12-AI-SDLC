import copy
import json
import unittest

from chonoi.errors import BadInput
from chonoi.ids import IdAllocator
from chonoi.inputs import InputState
from chonoi.rng import Streams
from chonoi.scenario import load_scenario
from chonoi.sim.quan import QuanSim
from tempfile import NamedTemporaryFile
from pathlib import Path

DISH = 'ca_loc_nuong_trui'
PAYLOAD = {'order_id': 'C1', 'dish_id': DISH, 'base_price': 60}


class QuanRules(unittest.TestCase):
    def sim(self, **scenario):
        with open('game.contract.json', encoding='utf-8') as f:
            tuning = json.load(f)['tuning']
        scenario.setdefault('random_customers', False)
        return QuanSim(tuning, scenario, Streams(1), IdAllocator())

    def customer(self, **changes):
        c = {'id': 'C1', 'table_id': 'T1', 'dish_id': DISH}
        c.update(changes)
        return c

    def step(self, sim, interact=False):
        return sim.step(InputState((0, 0), {}, {'interact'} if interact else set()))

    def test_full_cooking_serving_payment(self):
        s = self.sim(stock={'ca_loc': 1}, customers=[self.customer()], player={'pos': [2, 8]})
        ev = self.step(s, True)
        self.assertEqual(ev[0][0], 'cook_started')
        self.assertEqual(ev[0][1]['items'], ['ca_loc'])
        self.assertEqual(s.stock['ca_loc'], 0)
        for _ in range(359):
            self.assertNotIn('dish_ready', [n for n, _ in self.step(s)])
        ev = self.step(s, True)
        self.assertEqual([n for n, _ in ev], ['dish_ready', 'dish_picked'])
        self.assertEqual(s.hand, PAYLOAD)
        s.pos = [9, 2]
        ev = self.step(s, True)
        self.assertEqual(ev[0][0], 'served')
        self.assertEqual(ev[0][1]['tip'], 12)
        for _ in range(599):
            self.assertNotIn('paid', [n for n, _ in self.step(s)])
        self.assertEqual(self.step(s)[0][0], 'paid')
        self.assertEqual((s.money, s.earned, s.shift_earned), (72, 72, 72))
        self.assertAlmostEqual(s.reputation, 3.2)
        self.step(s)
        self.assertEqual(s.money, 72)

    def test_service_at_zero_patience_and_closing(self):
        s = self.sim(time_left_ticks=1, hand=copy.deepcopy(PAYLOAD), player={'pos': [9, 2]},
                     customers=[self.customer(order_status='assigned', patience_ticks_left=1)])
        ev = self.step(s, True)
        self.assertEqual([n for n, _ in ev], ['served', 'paid', 'quan_end'])
        self.assertEqual(ev[1][1]['reason'], 'closing')
        self.assertEqual(s.money, 60)
        self.assertEqual(s.reputation, 3)
        self.assertEqual(self.step(s, True), [])

    def test_tip_boundary_uses_decremented_patience(self):
        for patience, tip in ((1801, 12), (1800, 0)):
            s = self.sim(hand=copy.deepcopy(PAYLOAD), player={'pos': [9, 2]},
                         customers=[self.customer(order_status='assigned', patience_ticks_left=patience)])
            self.assertEqual(self.step(s, True)[0][1]['tip'], tip)

    def test_closing_waiter_releases_raw_reservation(self):
        s = self.sim(time_left_ticks=1, stock={'ca_loc': 1}, customers=[self.customer()])
        ev = self.step(s)
        self.assertEqual(ev[0][1]['reason'], 'closing')
        self.assertEqual(s.stock['ca_loc'], 1)
        self.assertEqual(s.reputation, 3)
        self.assertFalse(s.customers)

    def test_departure_discards_cooked_or_carried_dish(self):
        for location in ('hand', 'stoves'):
            kwargs = {location: copy.deepcopy(PAYLOAD) if location == 'hand' else
                      [{'id': 'B1', 'state': 'cooking', 'dish': copy.deepcopy(PAYLOAD), 'cook_ticks_left': 10}]}
            s = self.sim(customers=[self.customer(order_status='assigned', patience_ticks_left=1)], **kwargs)
            self.assertEqual(self.step(s)[0][0], 'customer_left')
            self.assertIsNone(s.hand)
            self.assertEqual(s.stoves[0]['state'], 'idle')
            self.assertEqual(s.reputation, 2.5)
            self.assertFalse(s.stock)

    def test_nearest_inactive_target_blocks_fallback_and_ties_choose_stove(self):
        for table_pos, expected in (([2, 8], ['cook_started']), ([2.5, 8], [])):
            s = self.sim(stock={'ca_loc': 1}, customers=[self.customer()], player={'pos': [2.5, 8]},
                         tables=[{'id': 'T1', 'pos': table_pos}])
            self.assertEqual([n for n, _ in self.step(s, True)], expected)

    def test_arrival_timing_and_full_initial_patience(self):
        s = self.sim(stock={'ca_loc': 1}, random_customers=True)
        for _ in range(119):
            self.assertFalse(self.step(s))
        self.assertEqual(self.step(s)[0][0], 'customer_arrived')
        self.assertEqual(s.customers[0]['patience_ticks_left'], 3600)
        self.assertEqual(s.stock['ca_loc'], 1)

    def test_invalid_fixture_rejected(self):
        cases = [dict(customers=[self.customer()]),
                 dict(customers=[self.customer(order_status='assigned')]),
                 dict(hand=copy.deepcopy(PAYLOAD)),
                 dict(customers=[self.customer(state='eating', order_status='served', eat_ticks_left=1)])]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(BadInput):
                self.sim(**case)

    def test_malformed_dish_identifiers_are_bad_input(self):
        for fixture in (
            {'customers': [self.customer(dish_id=[])]},
            {'customers': [self.customer(order_status='assigned')],
             'hand': {'order_id': [], 'dish_id': DISH, 'base_price': 60}},
        ):
            with self.subTest(fixture=fixture), self.assertRaises(BadInput):
                self.sim(**fixture)

    def test_scenario_rejects_invalid_furniture_updates(self):
        with open('game.contract.json', encoding='utf-8') as f:
            tuning = json.load(f)['tuning']
        for update in ({'stoves': [{'id': 'B1', 'pos': [99, 8]}]},
                       {'tables': [{'id': 'T1', 'customer_id': 'C1'}]},
                       {'random_customers': 1}):
            with NamedTemporaryFile(mode='w', suffix='.json', encoding='utf-8', delete=False) as f:
                json.dump({'quan': update}, f)
                path = f.name
            try:
                with self.subTest(update=update), self.assertRaises(BadInput):
                    load_scenario(path, 'quan', tuning)
            finally:
                Path(path).unlink()

    def test_same_seed_same_events(self):
        a = self.sim(stock={'ca_loc': 5, 'bong_sung': 5}, random_customers=True)
        b = self.sim(stock={'ca_loc': 5, 'bong_sung': 5}, random_customers=True)
        for _ in range(4000):
            self.assertEqual(self.step(a), self.step(b))
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_ready_age_and_radius_edge(self):
        s = self.sim(player={'pos': [3.5, 8]},
                     customers=[self.customer(order_status='assigned')],
                     stoves=[{'id': 'B1', 'state': 'ready', 'dish': copy.deepcopy(PAYLOAD)}])
        self.step(s)
        self.assertEqual(s.stoves[0]['ready_ticks'], 1)
        self.assertEqual(self.step(s, True)[0][0], 'dish_picked')

    def test_order_identity_required_for_service(self):
        s = self.sim(hand=copy.deepcopy(PAYLOAD), player={'pos': [9, 2]},
                     customers=[self.customer(order_status='assigned')])
        s.hand['order_id'] = 'C2'
        self.assertEqual(self.step(s, True), [])
        self.assertEqual(s.customers[0]['state'], 'waiting')

    def test_finished_eating_wins_over_closing_and_reputation_is_clamped(self):
        s = self.sim(time_left_ticks=1, reputation=4.9,
                     customers=[self.customer(state='eating', order_status='served',
                                               eat_ticks_left=1, base_price=60, tip=12)])
        events = self.step(s)
        self.assertEqual(events[0][1]['reason'], 'finished_eating')
        self.assertEqual(s.reputation, 5)

    def test_no_dish_and_no_arrival_on_closing_tick(self):
        s = self.sim(random_customers=True)
        for _ in range(119):
            self.step(s)
        self.assertEqual(self.step(s), [('no_dish', {})])
        s = self.sim(time_left_ticks=120, random_customers=True, stock={'ca_loc': 1})
        for _ in range(119):
            self.step(s)
        self.assertEqual([n for n, _ in self.step(s)], ['quan_end'])
