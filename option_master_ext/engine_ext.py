import typing
from typing import Optional, Dict, List, Set, Callable, Tuple
from copy import copy

from vnpy.event import Event, EventEngine
from vnpy.trader.event import (
    EVENT_TIMER, EVENT_ORDER
)
from vnpy.trader.constant import (
    Status, Direction, Offset
)
from vnpy.trader.object import (
    OrderData, OrderRequest, OrderType
)
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.app.option_master.engine import OptionEngine
from vnpy.app.option_master.base import (
    CHAIN_UNDERLYING_MAP,
    OptionData, PortfolioData, UnderlyingData, ChainData
)

APP_NAME = "OptionMasterExt"

EVENT_OPTION_HEDGE_STATUS = "eOptionHedgeStatus"


class OptionEngineExt(OptionEngine):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        super().__init__(main_engine, event_engine)
        self.engine_name = APP_NAME

        self.hedge_engine: "HedgeEngine" = HedgeEngine(self)


class HedgeEngine:
    def __init__(self, option_engine: OptionEngineExt):
        self.option_engine: OptionEngineExt = option_engine
        self.main_engine: MainEngine = option_engine.main_engine
        self.event_engine: EventEngine = option_engine.event_engine
        self.chase_order_engine: ChaseOrderEngine = ChaseOrderEngine(self.option_engine)

        # parameters
        self.check_delta_trigger: int = 5
        self.calc_balance_trigger: int = 300
        self.chanel_width: float = 0.0
        self.hedge_percent: float = 0.0

        # variables
        self.balance_prices: Dict[str, float] = {}
        self.underlyings: Dict[str, UnderlyingData] = {}
        self.underlying_symbols: Dict[str, str] = {}
        self.synthesis_chain_symbols: Dict[str, str] = {}
        self.auto_portfolio_names: List[str] = []
        self.counters: Dict[str, float] = {}
        self.auto_hedge_flags: Dict[str, bool] = {}
        self.hedge_parameters: Dict[str, Dict] = {}

        self.balance_price: float = 0.0

        # order funcitons
        self.buy: Optional[Callable] = None
        self.sell: Optional[Callable] = None
        self.short: Optional[Callable] = None
        self.cover: Optional[Callable] = None

        # init
        self.init_counter()
        self.add_order_function()

    def add_order_function(self) -> None:
        self.buy = self.chase_order_engine.buy
        self.sell = self.chase_order_engine.sell
        self.short = self.chase_order_engine.short
        self.cover = self.chase_order_engine.cover

    def start_auto_hedge(self, portfolio_name: str, params: Dict):
        self.hedge_parameters[portfolio_name] = params
        if self.is_auto_hedge(portfolio_name):
            return
        self.auto_hedge_flags[portfolio_name] = True
        self.put_hedge_status_event()
        self.write_log(f"组合{portfolio_name}自动对冲已启动")

    def stop_auto_hedge(self, portfolio_name: str):
        if not self.is_auto_hedge(portfolio_name):
            return
        self.auto_hedge_flags[portfolio_name] = False
        self.put_hedge_status_event()
        self.write_log(f"组合{portfolio_name}自动对冲已停止")

    def init_counter(self) -> None:
        self.counters['check_delta'] = 0
        self.counters['calculate_balance'] = 0

    def is_auto_hedge(self, portfolio_name: str) -> bool:
        flag = self.auto_hedge_flags.get(portfolio_name)
        if flag is None:
            self.auto_hedge_flags[portfolio_name] = False
        return flag

    def register_event(self) -> None:
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_timer_event(self, event: Event) -> None:
        check_delta_counter = self.counters.get('check_delta')
        calc_balance_counter = self.counters.get('calculate_balance')

        if check_delta_counter > self.check_delta_trigger:
            self.auto_hedge()
            check_delta_counter = 0

        if calc_balance_counter > self.calc_balance_trigger:
            self.calc_all_balance()
            calc_balance_counter = 0

        check_delta_counter += 1
        calc_balance_counter += 1

    def set_underlying_symbol(self, portfolio_name: str, underlying_symbol: str):
        self.underlying_symbols[portfolio_name] = underlying_symbol

    def set_synthesis_chain_symbol(self, portfolio_name: str, chain_symbol: str):
        self.synthesis_chain_symbols[portfolio_name] = chain_symbol

    def get_portfolio(self, portfolio_name: str) -> PortfolioData:
        active_portfolios = self.option_engine.active_portfolios
        portfolio = active_portfolios.get(portfolio_name)
        if not portfolio:
            self.write_log(f"通道对冲模块找不到组合{portfolio_name}")
        return portfolio

    def get_underlying(self, portfolio_name: str) -> UnderlyingData:
        underlying = self.underlyings.get(portfolio_name)

        if not underlying:
            portfolio = self.get_portfolio(portfolio_name)
            if not portfolio:
                return

            symbol = self.underlying_symbols.get(portfolio_name)
            if not symbol:
                self.write_log(f"找不到组合{portfolio_name}对应标的代码")
                return
            underlying = portfolio.underlyings.get(symbol)
            if not underlying:
                self.write_log(f"找不到组合{portfolio_name}对应标的{symbol}")
                return
            self.underlyings[portfolio_name] = underlying

        return self.underlyings[portfolio_name]
    
    def get_balance_price(self, portfolio_name: str) -> float:
        price = self.balance_prices.get(portfolio_name)
        if not price:
            self.calculate_balance_price(portfolio_name)
        return price

    def get_synthesis_chain(self, portfolio_name) -> ChainData:
        portfolio = self.get_portfolio(portfolio_name)
        if not portfolio:
            return

        chain_symbol = self.synthesis_chain_symbols.get(portfolio_name)
        chain = portfolio.get_chain(chain_symbol)
        return chain

    def calculate_pos_delta(self, portfolio_name: str, price: float) -> float:
        portfolio = self.get_portfolio(portfolio_name)
        if not portfolio:
            return

        portfolio_delta = 0
        for option in portfolio.options.values():
            if option.net_pos:
                _price, delta, _gamma, _theta, _vega = option.calculate_greeks(
                    price,
                    option.strike_price,
                    option.interest_rate,
                    option.time_to_expiry,
                    option.mid_impv,
                    option.option_type
                )
                delta = delta * option.size * option.net_pos
                portfolio_delta += delta
        return portfolio_delta

    def calculate_balance_price(self, portfolio_name: str) -> None:
        underlying = self.get_underlying(portfolio_name)
        if not underlying:
            return

        price = underlying.mid_price
        delta = self.calculate_pos_delta(portfolio_name, price)

        if delta > 0:
            while True:
                last_price = price
                price += price * 0.003
                delta = self.calculate_pos_delta(portfolio_name, price)
                if delta <= 0:
                    balance_price = (last_price + price) / 2
                    self.balance_prices[portfolio_name] = balance_price 
        else:
            while True:
                last_price = price
                price -= price * 0.003
                delta = self.calculate_pos_delta(portfolio_name, price)
                if delta >= 0:
                    balance_price = (last_price + price) / 2
                    self.balance_prices[portfolio_name] = balance_price

    def calc_all_balance(self) -> None:
        for portfolio_name in self.auto_portfolio_names:
            self.calculate_balance_price(portfolio_name)
    
    def auto_hedge(self) -> None:
        for portfolio_name in self.auto_portfolio_names:
            if not self.is_auto_hedge(portfolio_name):
                continue
            
            hedge_params = self.hedge_parameters.get(portfolio_name)

            balance_price = self.get_balance_price(portfolio_name)
            up = balance_price * (1 + hedge_params['offset_percent'])
            down = balance_price * (1 - hedge_params['offset_percent'])

            underlying = self.get_underlying(portfolio_name)
            if underlying.tick > up:
                self.action_hedge(portfolio_name, Direction.LONG)
            elif underlying.tick < down:
                self.action_hedge(portfolio_name, Direction.SHORT)
            else:
                continue

    def get_synthesis_atm(self, portfolio_name: str) -> Tuple[OptionData, OptionData]:
        chain = self.get_synthesis_chain(portfolio_name)
        atm_call = chain.calls[chain.atm_index]
        atm_put = chain.puts[chain.atm_index]
        return atm_call, atm_put

    def calc_hedge_volume(self, portfolio_name: str) -> int:
        atm_call, atm_put = self.get_synthesis_atm(portfolio_name)
        unit_hedge_delta = abs(atm_call.cash_delta) + abs(atm_put.cash_delta)

        hedge_params = self.hedge_parameters.get(portfolio_name)
        hedge_percent = hedge_params['hedge_percent']

        portfolio = self.get_portfolio(portfolio_name)
        to_hedge_volume = abs(portfolio.pos_delta) * hedge_percent / unit_hedge_delta
        return round(to_hedge_volume)

    def action_hedge(self, portfolio_name: str, direction: Direction):
        atm_call, atm_put = self.get_synthesis_atm(portfolio_name)
        to_hedge_volume = self.calc_hedge_volume(portfolio_name)
        if not to_hedge_volume:
            self.write_log(f"{portfolio_name} Delta偏移量少于最小对冲单元值")
            return

        if direction == Direction.LONG:
            self.buy(atm_call.vt_symbol, to_hedge_volume)
            self.sell(atm_put.vt_symbol, to_hedge_volume)
        elif direction == Direction.SHORT:
            self.buy(atm_put.vt_symbol, to_hedge_volume)
            self.sell(atm_call.vt_symbol, to_hedge_volume)
        else:
            self.write_log(f"对冲只支持多或者空")

    def put_hedge_status_event(self) -> None:
        status = copy(self.auto_hedge_flags)
        event = Event(EVENT_OPTION_HEDGE_STATUS, status)
        self.event_engine.put(event)

    def write_log(self, msg: str):
        self.main_engine.write_log(msg, source=APP_NAME)


class ChannelHedgeAlgo:

    def __init__(self, hedge_engine: HedgeEngine, portfolio: PortfolioData):
        self.hedge_engine = hedge_engine
        self.portfolio = portfolio

        self.portfolio_name: str = portfolio.name
        self.underlying: Optional[UnderlyingData] = None 
        self.chain_symbol: str = ''


        # parameters
        self.offset_percent: float = 0.0
        self.hedge_percent: float = 0.0

        # variables


class ChaseOrderEngine:
    def __init__(self, option_engine: OptionEngineExt):
        self.option_engine: OptionEngineExt = option_engine
        self.main_engine: MainEngine = option_engine.main_engine
        self.event_engine: EventEngine = option_engine.event_engine

        self.active_orderids: Set[str] = set()
        
        self.pay_up: int = 0
        self.cancel_interval: int = 3
        self.max_volume: int = 30
        
        self.cancel_counts: Dict[str, int] = {}

        self.register_event()

    def register_event(self) -> None:
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_order_event(self, event: Event) -> None:
        order: OrderData = event.data

        if order.vt_orderid not in self.active_orderids:
            return

        if not order.is_active():
            self.active_orderids.remove(order.vt_orderid)
            self.cancel_counts.pop(order.vt_orderid, None)

        if order.status == Status.CANCELLED:
            self.resend_order(order)

    def process_timer_event(self, event: Event) -> None:
        self.check_cancel()

    def resend_order(self, order: OrderData) -> None:
        new_volume = order.volume - order.traded
        self.send_order(order.vt_symbol, order.direction, order.offset, new_volume)

    def cancel_order(self, vt_orderid: str) -> None:
        order = self.main_engine.get_order(vt_orderid)
        req = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

    def check_cancel(self) -> None:
        for vt_orderid in self.active_orderids:
            if self.cancel_counts[vt_orderid] > self.cancel_interval:
                self.cancel_counts[vt_orderid] = 0
                self.cancel_order(vt_orderid)
            self.cancel_counts[vt_orderid] += 1

    def split_req(self, req: OrderRequest):
        if req.volume <= self.max_volume:
            return [req]

        max_count, remainder = divmod(req.volume, self.max_volume)

        req_max = copy(req)
        req_max.volume = self.max_volume
        req_list = [req_max for i in range(int(max_count))]

        if remainder:
            req_r = copy(req)
            req_r.volume = remainder
            req_list.append(req_r)
        return req_list

    def send_order(self, vt_symbol: str, direction: Direction, offset: Offset, volume: float, price: Optional[float] = None) -> str:
        contract = self.main_engine.get_contract(vt_symbol)
        tick = self.main_engine.get_tick(vt_symbol)

        if not price:
            if direction == Direction.LONG:
                price = tick.ask_price_1 + contract.pricetick * self.pay_up
            else:
                price = tick.bid_price_1 - contract.pricetick * self.pay_up

        original_req = OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            type=OrderType.LIMIT,
            volume=volume,
            price=price
        )

        splited_req_list = self.split_req(original_req)

        vt_orderids = []
        for req in splited_req_list:
            vt_orderid = self.main_engine.send_order(req, contract.gateway_name)
            vt_orderids.append(vt_orderid)
            self.active_orderids.add(vt_orderid)
            self.cancel_counts[vt_orderid] = 0

        return vt_orderids

    def buy(self, vt_symbol: str, volume: float, price: Optional[float] = None):
        return self.send_order(vt_symbol, Direction.LONG, Offset.OPEN, volume, price)

    def sell(self, vt_symbol: str, volume: float, price: Optional[float] = None):
        return self.send_order(vt_symbol, Direction.SHORT, Offset.CLOSE, volume, price)

    def short(self, vt_symbol: str, volume: float, price: Optional[float] = None):
        return self.send_order(vt_symbol, Direction.SHORT, Offset.OPEN, volume, price)

    def cover(self, vt_symbol: str, volume: float, price: Optional[float] = None):
        return self.send_order(vt_symbol, Direction.LONG, Offset.CLOSE, volume, price)