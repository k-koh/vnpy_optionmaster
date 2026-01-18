from datetime import datetime
from collections.abc import Callable
from types import ModuleType
from functools import lru_cache

from vnpy.event import EventEngine
from vnpy.event.engine import Event
from vnpy.trader.event import EVENT_TICK, EVENT_ATM, EVENT_VI
from vnpy.trader.object import ContractData, TickData, TradeData, BarData, AtmData, ViData
from vnpy.trader.constant import Exchange, OptionType, Direction, Offset, OptionPrevIvType
from vnpy.trader.converter import PositionHolding
from vnpy.trader.utility import extract_vt_symbol

from .time import calculate_days_to_expiry, ANNUAL_DAYS


APP_NAME = "OptionMaster"

EVENT_OPTION_NEW_PORTFOLIO = "eOptionNewPortfolio"
EVENT_OPTION_ALGO_PRICING = "eOptionAlgoPricing"
EVENT_OPTION_ALGO_TRADING = "eOptionAlgoTrading"
EVENT_OPTION_ALGO_STATUS = "eOptionAlgoStatus"
EVENT_OPTION_ALGO_LOG = "eOptionAlgoLog"
EVENT_OPTION_RISK_NOTICE = "eOptionRiskNotice"
EVENT_OPTION_INSTRUMENT_ADD = "eOptionInstrumentAdd"


class InstrumentData:
    """"""

    def __init__(self, contract: ContractData) -> None:
        """"""
        self.symbol: str = contract.symbol
        self.exchange: Exchange = contract.exchange
        self.vt_symbol: str = contract.vt_symbol

        self.pricetick: float = contract.pricetick
        self.min_volume: float = contract.min_volume
        self.size: int = contract.size

        self.long_pos: int = 0
        self.short_pos: int = 0
        self.net_pos: int = 0
        self.mid_price: float = 0

        self.tick: TickData | None = None
        self.portfolio: PortfolioData

    def calculate_net_pos(self) -> None:
        """"""
        self.net_pos = self.long_pos - self.short_pos

    def update_tick(self, tick: TickData) -> None:
        """"""
        self.tick = tick
        self.mid_price = (tick.bid_price_1 + tick.ask_price_1) / 2

    def update_trade(self, trade: TradeData) -> None:
        """"""
        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN:
                self.long_pos += trade.volume
            else:
                self.short_pos -= trade.volume
        else:
            if trade.offset == Offset.OPEN:
                self.short_pos += trade.volume
            else:
                self.long_pos -= trade.volume
        self.calculate_net_pos()

    def update_holding(self, holding: PositionHolding) -> None:
        """"""
        self.long_pos = holding.long_pos
        self.short_pos = holding.short_pos
        self.calculate_net_pos()

    def set_portfolio(self, portfolio: "PortfolioData") -> None:
        """"""
        self.portfolio = portfolio


class OptionData(InstrumentData):
    """"""

    def __init__(self, contract: ContractData) -> None:
        """"""
        super().__init__(contract)

        # Option contract features
        self.strike_price: float = contract.option_strike
        self.chain_index: str = contract.option_index

        self.option_type: int = 0
        if contract.option_type == OptionType.CALL:
            self.option_type = 1
        else:
            self.option_type = -1

        self.option_expiry: datetime = contract.option_expiry
        self.days_to_expiry: int = calculate_days_to_expiry(
            contract.option_expiry
        )
        self.time_to_expiry: float = self.days_to_expiry / ANNUAL_DAYS

        self.interest_rate: float = 0

        # Option portfolio related
        self.underlying: UnderlyingData
        self.chain: ChainData
        self.underlying_adjustment: float = 0

        # Pricing model
        self.calculate_price: Callable
        self.calculate_greeks: Callable
        self.calculate_impv: Callable

        # Implied volatility
        self.bid_impv: float = 0
        self.ask_impv: float = 0
        self.mid_impv: float = 0
        self.pricing_impv: float = 0

        # Greeks related
        self.theo_delta: float = 0
        self.theo_gamma: float = 0
        self.theo_theta: float = 0
        self.theo_vega: float = 0

        self.pos_value: float = 0
        self.pos_delta: float = 0
        self.pos_gamma: float = 0
        self.pos_theta: float = 0
        self.pos_vega: float = 0

    def calculate_option_impv(self) -> None:
        """"""
        if not self.tick or not hasattr(self, "underlying") or not self.underlying:
            return

        underlying_price: float = self.underlying.mid_price
        if not underlying_price:
            return
        underlying_price += self.underlying_adjustment

        ask_price: float = self.tick.ask_price_1
        bid_price: float = self.tick.bid_price_1

        if ask_price and bid_price:
            mid_price: float = (ask_price + bid_price) / 2
        elif ask_price:
            mid_price = ask_price
        elif bid_price:
            mid_price = bid_price
        else:
            mid_price = 0

        self.ask_impv = self.calculate_impv(
            ask_price,
            underlying_price,
            self.strike_price,
            self.interest_rate,
            self.time_to_expiry,
            self.option_type
        )

        self.bid_impv = self.calculate_impv(
            bid_price,
            underlying_price,
            self.strike_price,
            self.interest_rate,
            self.time_to_expiry,
            self.option_type
        )

        self.mid_impv = self.calculate_impv(
            mid_price,
            underlying_price,
            self.strike_price,
            self.interest_rate,
            self.time_to_expiry,
            self.option_type
        )

    def calculate_theo_greeks(self) -> None:
        """"""
        if not hasattr(self, "underlying") or not self.underlying:
            return

        underlying_price: float = self.underlying.mid_price
        if not underlying_price or not self.mid_impv:
            return
        underlying_price += self.underlying_adjustment

        _, delta, gamma, theta, vega = self.calculate_greeks(
            underlying_price,
            self.strike_price,
            self.interest_rate,
            self.time_to_expiry,
            self.mid_impv,
            self.option_type
        )

        self.theo_delta = delta * self.size
        self.theo_gamma = gamma * self.size
        self.theo_theta = theta * self.size / 240
        self.theo_vega = vega * self.size / 100

    def calculate_pos_greeks(self) -> None:
        """"""
        if self.tick:
            self.pos_value = self.tick.last_price * self.size * self.net_pos

        self.pos_delta = self.theo_delta * self.net_pos
        self.pos_gamma = self.theo_gamma * self.net_pos
        self.pos_theta = self.theo_theta * self.net_pos
        self.pos_vega = self.theo_vega * self.net_pos

    def calculate_ref_price(self) -> float:
        """"""
        underlying_price: float = self.underlying.mid_price
        underlying_price += self.underlying_adjustment

        ref_price: float = self.calculate_price(
            underlying_price,
            self.strike_price,
            self.interest_rate,
            self.time_to_expiry,
            self.pricing_impv,
            self.option_type
        )

        return ref_price

    def update_tick(self, tick: TickData) -> None:
        """"""
        super().update_tick(tick)

        self.calculate_option_impv()

    def update_trade(self, trade: TradeData) -> None:
        """"""
        super().update_trade(trade)
        self.calculate_pos_greeks()

    def update_underlying_tick(self, underlying_adjustment: float) -> None:
        """"""
        self.underlying_adjustment = underlying_adjustment

        self.calculate_option_impv()
        self.calculate_theo_greeks()
        self.calculate_pos_greeks()

    def set_chain(self, chain: "ChainData") -> None:
        """"""
        self.chain = chain

    def set_underlying(self, underlying: "UnderlyingData") -> None:
        """"""
        self.underlying = underlying

    def set_interest_rate(self, interest_rate: float) -> None:
        """"""
        self.interest_rate = interest_rate

    def set_pricing_model(self, pricing_model: ModuleType) -> None:
        """"""
        self.calculate_greeks = pricing_model.calculate_greeks
        self.calculate_impv = pricing_model.calculate_impv
        self.calculate_price = pricing_model.calculate_price


class UnderlyingData(InstrumentData):
    """"""

    def __init__(self, contract: ContractData) -> None:
        """"""
        super().__init__(contract)

        self.theo_delta: float = self.size                  # 标的物理论Delta固定为1
        self.pos_delta: float = 0
        self.chains: dict[str, ChainData] = {}

    def add_chain(self, chain: "ChainData") -> None:
        """"""
        self.chains[chain.chain_symbol] = chain

    def update_tick(self, tick: TickData) -> None:
        """"""
        super().update_tick(tick)

        for chain in self.chains.values():
            chain.update_underlying_tick()

        self.calculate_pos_greeks()

    def update_trade(self, trade: TradeData) -> None:
        """"""
        super().update_trade(trade)

        self.calculate_pos_greeks()

    def calculate_pos_greeks(self) -> None:
        """"""
        self.pos_delta = self.theo_delta * self.net_pos


class ChainData:
    """"""

    def __init__(self, chain_symbol: str, event_engine: EventEngine) -> None:
        """"""
        self.chain_symbol: str = chain_symbol
        self.event_engine: EventEngine = event_engine

        self.long_pos: int = 0
        self.short_pos: int = 0
        self.net_pos: int = 0

        self.pos_value: float = 0
        self.pos_delta: float = 0
        self.pos_gamma: float = 0
        self.pos_theta: float = 0
        self.pos_vega: float = 0

        self.underlying: UnderlyingData

        self.options: dict[str, OptionData] = {}
        self.calls: dict[str, OptionData] = {}
        self.puts: dict[str, OptionData] = {}

        self.portfolio: PortfolioData

        self.indexes: list[str] = []
        self.atm_price: float = 0
        self.atm_index: str = ""
        self.pre_atm_index: str = ""
        self.underlying_adjustment: float = 0
        self.days_to_expiry: int = 0

        self.use_synthetic: bool = False
        self.atm_impv: float | None = None
        self.atm_strike: int | None = None

        self.eris_p_iv: float | None = None
        self.eris_p_strike: int | None = None
        self.eris_c_iv: float | None = None
        self.eris_c_strike: int | None = None
        self.delta022_c_iv: float | None = None      # Call Δ0.22 iv
        self.delta022_c_strike: int | None = None  # Call Δ0.22 strike
        self.delta002_c_iv: float | None = None  # Call Δ0.02 iv
        self.delta002_c_strike: int | None = None  # Call Δ0.02 strike
        self.delta012_p_iv: float | None = None      # Put Δ0.12 iv
        self.delta012_p_strike: int | None = None  # Put Δ0.12 strike

    def add_option(self, option: OptionData) -> None:
        """"""
        self.options[option.vt_symbol] = option

        if option.option_type > 0:
            self.calls[option.chain_index] = option
        else:
            self.puts[option.chain_index] = option

        option.set_chain(self)

        if option.chain_index not in self.indexes:
            self.indexes.append(option.chain_index)

            # Sort index by number if possible, otherwise by string
            try:
                float(option.chain_index)
                self.indexes.sort(key=float)
            except ValueError:
                self.indexes.sort()

        self.days_to_expiry = option.days_to_expiry

    def calculate_pos_greeks(self) -> None:
        """"""
        # Clear data
        self.long_pos = 0
        self.short_pos = 0
        self.net_pos = 0
        self.pos_value = 0
        self.pos_delta = 0
        self.pos_gamma = 0
        self.pos_theta = 0
        self.pos_vega = 0

        # Sum all value
        for option in self.options.values():
            if option.net_pos:
                self.long_pos += option.long_pos
                self.short_pos += option.short_pos
                self.pos_value += option.pos_value
                self.pos_delta += option.pos_delta
                self.pos_gamma += option.pos_gamma
                self.pos_theta += option.pos_theta
                self.pos_vega += option.pos_vega

        self.net_pos = self.long_pos - self.short_pos

    def update_tick(self, tick: TickData) -> None:
        """"""
        option: OptionData = self.options[tick.vt_symbol]
        option.update_tick(tick)

        if self.use_synthetic:
            if not self.atm_index:
                self.calculate_atm_price()

            if option.chain_index == self.atm_index:
                self.update_synthetic_price()

    def update_underlying_tick(self) -> None:
        """"""
        if not self.use_synthetic:
            self.calculate_underlying_adjustment()

        for option in self.options.values():
            option.update_underlying_tick(self.underlying_adjustment)

        self.calculate_pos_greeks()
        self.calculate_eris_data()

    def update_trade(self, trade: TradeData) -> None:
        """"""
        option: OptionData = self.options[trade.vt_symbol]

        # Deduct old option pos greeks
        self.long_pos -= option.long_pos
        self.short_pos -= option.short_pos
        self.pos_value -= option.pos_value
        self.pos_delta -= option.pos_delta
        self.pos_gamma -= option.pos_gamma
        self.pos_theta -= option.pos_theta
        self.pos_vega -= option.pos_vega

        # Calculate new option pos greeks
        option.update_trade(trade)

        # Add new option pos greeks
        self.long_pos += option.long_pos
        self.short_pos += option.short_pos
        self.pos_value += option.pos_value
        self.pos_delta += option.pos_delta
        self.pos_gamma += option.pos_gamma
        self.pos_theta += option.pos_theta
        self.pos_vega += option.pos_vega

        self.net_pos = self.long_pos - self.short_pos

    def set_underlying(self, underlying: "UnderlyingData") -> None:
        """"""
        underlying.add_chain(self)
        self.underlying = underlying

        for option in self.options.values():
            option.set_underlying(underlying)

        if underlying.exchange == Exchange.LOCAL:
            self.use_synthetic = True

    def set_interest_rate(self, interest_rate: float) -> None:
        """"""
        for option in self.options.values():
            option.set_interest_rate(interest_rate)

    def set_pricing_model(self, pricing_model: ModuleType) -> None:
        """"""
        for option in self.options.values():
            option.set_pricing_model(pricing_model)

    def set_portfolio(self, portfolio: "PortfolioData") -> None:
        """"""
        for option in self.options.values():
            option.set_portfolio(portfolio)

    def put_atm_event(self, atm: AtmData) -> None:
        """"""
        event: Event = Event(EVENT_ATM, atm)
        self.event_engine.put(event)

    def calculate_atm_price(self) -> None:
        """"""
        min_diff: float = 0
        atm_price: float = 0
        atm_index: str = ""

        for index, call in self.calls.items():
            put: OptionData = self.puts.get(index)
            if not put:
                continue

            if call.strike_price % 1000 != 0:
                continue

            call_tick: TickData = call.tick
            if not call_tick or not call_tick.bid_price_1 or not call_tick.ask_price_1:
                continue

            put_tick: TickData = put.tick
            if not put_tick or not put_tick.bid_price_1 or not put_tick.ask_price_1:
                continue

            call_mid_price: float = (call_tick.ask_price_1 + call_tick.bid_price_1) / 2
            put_mid_price: float = (put_tick.ask_price_1 + put_tick.bid_price_1) / 2

            diff: float = abs(call_mid_price - put_mid_price)

            if not min_diff or diff < min_diff:
                min_diff = diff
                atm_price = call.strike_price
                atm_index = call.chain_index

        self.atm_price = atm_price
        self.atm_index = atm_index
        if self.pre_atm_index != self.atm_index:
            self.pre_atm_index = self.atm_index
            if atm_index:
                atm: AtmData = AtmData(chain_symbol=self.chain_symbol, atm_strike=int(float(self.atm_index)))
                self.put_atm_event(atm)

        self.calculate_atm_impv()

    def calculate_atm_impv(self) -> None:
        """"""
        if not self.atm_index:
            self.atm_impv = 0
            return

        atm_call: OptionData = self.calls.get(self.atm_index)
        atm_put: OptionData = self.puts.get(self.atm_index)

        if atm_call and atm_put:
            self.atm_impv = (atm_call.mid_impv + atm_put.mid_impv) / 2
        elif atm_call:
            self.atm_impv = atm_call.mid_impv
        elif atm_put:
            self.atm_impv = atm_put.mid_impv
        else:
            self.atm_impv = 0

    def calculate_eris_data(self) -> None:
        """
        Calculate ERIS data (IV and strike for options with specific deltas).
        """
        # Find call with delta closest to +0.1
        min_call_delta_diff = 100.0
        eris_call = None

        for call in self.calls.values():
            if not call.theo_delta or not call.size:
                continue

            if call.strike_price % 1000 != 0:
                continue

            option_delta = call.theo_delta / call.size
            delta_diff = abs(option_delta - 0.1)

            if delta_diff < min_call_delta_diff:
                min_call_delta_diff = delta_diff
                eris_call = call

        if eris_call:
            self.eris_c_iv = eris_call.mid_impv
            self.eris_c_strike = eris_call.strike_price
        else:
            self.eris_c_iv = None
            self.eris_c_strike = None

        # Find put with delta closest to -0.1
        min_put_delta_diff = 100.0
        eris_put = None

        for put in self.puts.values():
            if not put.theo_delta or not put.size:
                continue

            if put.strike_price % 1000 != 0:
                continue

            option_delta = put.theo_delta / put.size
            delta_diff = abs(option_delta - (-0.1))

            if delta_diff < min_put_delta_diff:
                min_put_delta_diff = delta_diff
                eris_put = put

        if eris_put:
            self.eris_p_iv = eris_put.mid_impv
            self.eris_p_strike = eris_put.strike_price
        else:
            self.eris_p_iv = None
            self.eris_p_strike = None

        # Find call with delta closest to +0.22
        min_call_delta_diff = 100.0
        delta022_call = None

        for call in self.calls.values():
            if not call.theo_delta or not call.size:
                continue

            if call.strike_price % 1000 != 0:
                continue

            option_delta = call.theo_delta / call.size
            delta_diff = abs(option_delta - 0.22)

            if delta_diff < min_call_delta_diff:
                min_call_delta_diff = delta_diff
                delta022_call = call

        if delta022_call:
            self.delta022_c_iv = delta022_call.mid_impv
            self.delta022_c_strike = delta022_call.strike_price
        else:
            self.delta022_c_iv = None
            self.delta022_c_strike = None

        # Find call with delta closest to +0.02
        min_call_delta_diff = 100.0
        delta002_call = None

        for call in self.calls.values():
            if not call.theo_delta or not call.size:
                continue

            if call.strike_price % 1000 != 0:
                continue

            option_delta = call.theo_delta / call.size
            delta_diff = abs(option_delta - 0.02)

            if delta_diff < min_call_delta_diff:
                min_call_delta_diff = delta_diff
                delta002_call = call

        if delta002_call:
            self.delta002_c_iv = delta002_call.mid_impv
            self.delta002_c_strike = delta002_call.strike_price
        else:
            self.delta002_c_iv = None
            self.delta002_c_strike = None

        # Find put with delta closest to -0.12
        min_put_delta_diff = 100.0
        delta012_put = None

        for put in self.puts.values():
            if not put.theo_delta or not put.size:
                continue

            if put.strike_price % 1000 != 0:
                continue

            option_delta = put.theo_delta / put.size
            delta_diff = abs(option_delta - (-0.12))

            if delta_diff < min_put_delta_diff:
                min_put_delta_diff = delta_diff
                delta012_put = put

        if delta012_put:
            self.delta012_p_iv = delta012_put.mid_impv
            self.delta012_p_strike = delta012_put.strike_price
        else:
            self.delta012_p_iv = None
            self.delta012_p_strike = None

    def calculate_underlying_adjustment(self) -> None:
        """"""
        if not self.atm_price or not self.atm_index:
            return

        atm_call: OptionData = self.calls[self.atm_index]
        atm_put: OptionData = self.puts[self.atm_index]

        call_price: float = atm_call.mid_price
        put_price: float = atm_put.mid_price

        synthetic_price: float = call_price - put_price + self.atm_price
        self.underlying_adjustment = synthetic_price - self.underlying.mid_price

    def update_synthetic_price(self) -> None:
        """"""
        if not self.atm_index:
            return

        call: OptionData = self.calls[self.atm_index]
        put: OptionData = self.puts[self.atm_index]

        self.underlying.mid_price = call.mid_price - put.mid_price + self.atm_price
        self.update_underlying_tick()

        # 推送合成期货的行情
        symbol, exchange = extract_vt_symbol(self.underlying.vt_symbol)

        tick: TickData = TickData(
            symbol=symbol,
            exchange=exchange,
            datetime=datetime.now(),
            last_price=self.underlying.mid_price,
            gateway_name=APP_NAME
        )
        event: Event = Event(EVENT_TICK + tick.vt_symbol, tick)
        self.event_engine.put(event)


class PortfolioData:

    def __init__(self, name: str, event_engine: EventEngine) -> None:
        """"""
        self.name: str = name
        self.event_engine: EventEngine = event_engine

        self.pricing_model: ModuleType | None = None
        self.interest_rate: float = 0.0

        self.long_pos: int = 0
        self.short_pos: int = 0
        self.net_pos: int = 0

        self.pos_delta: float = 0
        self.pos_gamma: float = 0
        self.pos_theta: float = 0
        self.pos_vega: float = 0

        # All instrument
        self._options: dict[str, OptionData] = {}
        self._chains: dict[str, ChainData] = {}

        # Active instrument
        self.options: dict[str, OptionData] = {}
        self.chains: dict[str, ChainData] = {}
        self.underlyings: dict[str, UnderlyingData] = {}

        # Greeks decimals precision
        self.precision: int = 0

    def calculate_pos_greeks(self) -> None:
        """"""
        self.long_pos = 0
        self.short_pos = 0
        self.net_pos = 0

        self.pos_value = 0.0
        self.pos_delta = 0
        self.pos_gamma = 0
        self.pos_theta = 0
        self.pos_vega = 0

        for underlying in self.underlyings.values():
            self.pos_delta += underlying.pos_delta

        for chain in self.chains.values():
            self.long_pos += chain.long_pos
            self.short_pos += chain.short_pos
            self.pos_value += chain.pos_value
            self.pos_delta += chain.pos_delta
            self.pos_gamma += chain.pos_gamma
            self.pos_theta += chain.pos_theta
            self.pos_vega += chain.pos_vega

        self.net_pos = self.long_pos - self.short_pos

    def update_tick(self, tick: TickData) -> None:
        """"""
        if tick.vt_symbol in self.options:
            option: OptionData = self.options[tick.vt_symbol]
            chain: ChainData = option.chain
            chain.update_tick(tick)
            self.calculate_pos_greeks()
        elif tick.vt_symbol in self.underlyings:
            underlying: UnderlyingData = self.underlyings[tick.vt_symbol]
            underlying.update_tick(tick)
            self.calculate_pos_greeks()

    def update_trade(self, trade: TradeData) -> None:
        """"""
        if trade.vt_symbol in self.options:
            option: OptionData = self.options[trade.vt_symbol]
            chain: ChainData = option.chain
            chain.update_trade(trade)
            self.calculate_pos_greeks()
        elif trade.vt_symbol in self.underlyings:
            underlying: UnderlyingData = self.underlyings[trade.vt_symbol]
            underlying.update_trade(trade)
            self.calculate_pos_greeks()

    def set_interest_rate(self, interest_rate: float) -> None:
        """"""
        self.interest_rate = interest_rate
        for chain in self.chains.values():
            chain.set_interest_rate(interest_rate)

    def set_pricing_model(self, pricing_model: ModuleType) -> None:
        """"""
        self.pricing_model = pricing_model
        for chain in self.chains.values():
            chain.set_pricing_model(pricing_model)

    def set_precision(self, precision: int) -> None:
        """"""
        self.precision = precision

    def set_chain_underlying(self, chain_symbol: str, contract: ContractData) -> None:
        """"""
        underlying: UnderlyingData | None = self.underlyings.get(contract.vt_symbol, None)
        if not underlying:
            underlying = UnderlyingData(contract)
            underlying.set_portfolio(self)
            self.underlyings[contract.vt_symbol] = underlying

        chain: ChainData = self.get_chain(chain_symbol)
        chain.set_underlying(underlying)

        # Add to active dict
        self.chains[chain_symbol] = chain

        for option in chain.options.values():
            self.options[option.vt_symbol] = option

    def get_chain(self, chain_symbol: str) -> ChainData:
        """"""
        chain: ChainData | None = self._chains.get(chain_symbol, None)

        if not chain:
            chain = ChainData(chain_symbol, self.event_engine)
            chain.set_portfolio(self)
            self._chains[chain_symbol] = chain

        return chain

    def add_option(self, contract: ContractData) -> None:
        """"""
        # Return if option already exists
        if contract.vt_symbol in self._options:
            return

        option: OptionData = OptionData(contract)
        option.set_portfolio(self)
        self._options[contract.vt_symbol] = option

        # Set model and rate if portfolio has them
        if self.pricing_model:
            option.set_pricing_model(self.pricing_model)
        if self.interest_rate:
            option.set_interest_rate(self.interest_rate)

        # Get chain and link it to option
        exchange_name: str = contract.exchange.value
        chain_symbol: str = f"{contract.option_underlying}.{exchange_name}"

        chain: ChainData = self.get_chain(chain_symbol)
        chain.add_option(option)

        # Link underlying to option if chain is already linked to an underlying
        if hasattr(chain, "underlying"):
            option.set_underlying(chain.underlying)

    def calculate_atm_price(self) -> None:
        """"""
        for chain in self.chains.values():
            chain.calculate_atm_price()


# 前日比用のOptionData
class PreviousDayOptionData:
    """"""
    def __init__(self) -> None:
        self.bars: dict[datetime, dict[str, BarData]] = {}
        self.eris_p_iv: dict[str, float | None] = {}
        self.eris_c_iv: dict[str, float | None] = {}
        self.delta002_c_iv: dict[str, float | None] = {}
        self.atm_iv: dict[str, float | None] = {}
        self.datetime: dict[int, datetime | None] = {}
        self.sorted_dates: list[datetime] = []
        self.op_months: set[str] = set()

    def sort_bar_datetime(self) -> None:
        """"""
        self.sorted_dates = sorted(self.bars.keys(), reverse=True)

    def get_prev_day_datetime(self, dt: datetime) -> datetime | None:
        """"""
        for i, date in enumerate(self.sorted_dates):
            if date < dt:
                return date
        return None

    def add_bar(self, bar: BarData) -> None:
        day_datetime = bar.datetime
        day_bars     = self.bars.setdefault(day_datetime, {})
        if not bar.vt_symbol in day_bars:
            day_bars[bar.vt_symbol] = bar
        symbols = bar.vt_symbol.split("-")
        self.op_months.add(symbols[0] + "-" + symbols[1])

    def calculate_eris_data(self) -> None:
        """
        Calculate ERIS IVs from stored bars.
        """
        bar_dates = list(self.bars.keys())
        for dt in bar_dates:
            day_bars = self.bars.get(dt, {})
            for op_month in self.op_months:
                # get bars for the specific option month
                month_bars = {k: v for k, v in day_bars.items() if k.startswith(op_month)}
                dt_str = dt.strftime("%Y-%m-%d-%H-%M-%S")
                dt_op_month = dt_str + "_" + op_month
                # Find put with delta closest to -0.1
                min_put_delta_diff = 100.0
                eris_put_bar = None

                for bar in month_bars.values():
                    if not hasattr(bar, 'delta'):
                        continue

                    option_delta = bar.delta
                    delta_diff = abs(option_delta - (-0.1))

                    if delta_diff < min_put_delta_diff:
                        min_put_delta_diff = delta_diff
                        eris_put_bar = bar

                if eris_put_bar and hasattr(eris_put_bar, 'iv'):
                    self.eris_p_iv[dt_op_month] = eris_put_bar.iv
                else:
                    self.eris_p_iv[dt_op_month] = 0

                # Find call with delta closest to +0.1
                min_call_delta_diff = 100.0
                eris_call_bar = None

                for bar in month_bars.values():
                    if not hasattr(bar, 'delta'):
                        continue

                    option_delta = bar.delta
                    delta_diff = abs(option_delta - 0.1)

                    if delta_diff < min_call_delta_diff:
                        min_call_delta_diff = delta_diff
                        eris_call_bar = bar

                if eris_call_bar and hasattr(eris_call_bar, 'iv'):
                    self.eris_c_iv[dt_op_month] = eris_call_bar.iv
                else:
                    self.eris_c_iv[dt_op_month] = 0

    def calculate_atm_iv(self) -> None:
        # Find put with delta closest to -0.5
        bar_dates = list(self.bars.keys())
        for dt in bar_dates:
            day_bars = self.bars.get(dt, {})
            for op_month in self.op_months:
                # get bars for the specific option month
                month_bars = {k: v for k, v in day_bars.items() if k.startswith(op_month)}
                dt_str = dt.strftime("%Y-%m-%d-%H-%M-%S")
                dt_op_month = dt_str + "_" + op_month

                min_put_delta_diff = 100.0
                atm_put_bar = None
                atm_put_iv = None

                for bar in month_bars.values():
                    if not hasattr(bar, 'delta'):
                        continue

                    option_delta = bar.delta
                    delta_diff = abs(option_delta - (-0.5))

                    if delta_diff < min_put_delta_diff:
                        min_put_delta_diff = delta_diff
                        atm_put_bar = bar

                if atm_put_bar and hasattr(atm_put_bar, 'iv'):
                    atm_put_iv = atm_put_bar.iv
                else:
                    atm_put_iv = None

                # Find call with delta closest to +0.5
                min_call_delta_diff = 100.0
                atm_call_bar = None
                atm_call_iv = None

                for bar in month_bars.values():
                    if not hasattr(bar, 'delta'):
                        continue

                    option_delta = bar.delta
                    delta_diff = abs(option_delta - 0.5)

                    if delta_diff < min_call_delta_diff:
                        min_call_delta_diff = delta_diff
                        atm_call_bar = bar

                if atm_call_bar and hasattr(atm_call_bar, 'iv'):
                    atm_call_iv = atm_call_bar.iv
                else:
                    atm_call_iv = None

                if atm_put_iv is not None and atm_call_iv is not None:
                    self.atm_iv[dt_op_month] = (atm_put_iv + atm_call_iv) / 2
                elif atm_put_iv is not None:
                    self.atm_iv[dt_op_month] = atm_put_iv
                elif atm_call_iv is not None:
                    self.atm_iv[dt_op_month] = atm_call_iv
                else:
                    self.atm_iv[dt_op_month] = 0

    def get_prev_day_option_iv(
        self,
        op_month: str,
        prev_iv_type: OptionPrevIvType,
        put_strike: int,
        call_strike: int,
        delta022_call_strike: int,
        atm_strike: int,
        dt: datetime
    ) -> tuple[float, float, float, float]:
        """"""
        put_iv: float = 0.0
        call_iv: float = 0.0
        delta002_call_iv: float = 0.0
        atm_iv: float = 0.0

        prev_date = self.get_prev_day_datetime(dt)
        if not prev_date:
            return put_iv, call_iv, delta002_call_iv, atm_iv

        if prev_iv_type == OptionPrevIvType.SAME_DELTA:
            dt_op_month = prev_date.strftime("%Y-%m-%d-%H-%M-%S") + "_" + op_month
            put_iv = self.eris_p_iv.get(dt_op_month, 0.0)
            call_iv = self.eris_c_iv.get(dt_op_month, 0.0)
            delta002_call_iv = self.delta002_c_iv.get(dt_op_month, 0.0)
            atm_iv = self.atm_iv.get(dt_op_month, 0.0)
        elif prev_iv_type == OptionPrevIvType.SAME_STRIKE:
            if put_strike is not None and call_strike is not None:
                day_bars = self.bars.get(prev_date, {})
                c_strike = int(call_strike)
                p_strike = int(put_strike)
                a_strike = int(atm_strike)
                put_vt_symbol = f"{op_month}-P-{p_strike}.JPX"
                call_vt_symbol = f"{op_month}-C-{c_strike}.JPX"
                atm_call_vt_symbol = f"{op_month}-C-{a_strike}.JPX"
                atm_put_vt_symbol = f"{op_month}-P-{a_strike}.JPX"
                # Get Put IV
                if put_vt_symbol in day_bars:
                    bar = day_bars[put_vt_symbol]
                    if hasattr(bar, 'iv'):
                        put_iv = bar.iv
                # Get Call IV
                if call_vt_symbol in day_bars:
                    bar = day_bars[call_vt_symbol]
                    if hasattr(bar, 'iv'):
                        call_iv = bar.iv
                # Get delta002 Call IV
                if delta022_call_strike is not None:
                    delta002_c_strike = int(delta022_call_strike)
                    delta002_call_vt_symbol = f"{op_month}-C-{delta002_c_strike}.JPX"
                    if delta002_call_vt_symbol in day_bars:
                        bar = day_bars[delta002_call_vt_symbol]
                        if hasattr(bar, 'iv'):
                            delta002_call_iv = bar.iv
                # Calculate ATM IV
                atm_call_bar = day_bars.get(atm_call_vt_symbol, None)
                atm_put_bar = day_bars.get(atm_put_vt_symbol, None)

                atm_call_iv = getattr(atm_call_bar, "iv", None)
                atm_put_iv = getattr(atm_put_bar, "iv", None)

                if atm_call_iv is not None and atm_put_iv is not None:
                    atm_iv = (atm_call_iv + atm_put_iv) / 2
                elif atm_call_iv is not None:
                    atm_iv = atm_call_iv
                elif atm_put_iv is not None:
                    atm_iv = atm_put_iv
        return put_iv, call_iv, delta002_call_iv, atm_iv

    def get_prev_day_iv_curve(
        self,
        dt: datetime,
        chain: "ChainData"
    ) -> tuple[dict[float, float], dict[float, float]]:
        """"""
        put_ivs: dict[float, float] = {}
        call_ivs: dict[float, float] = {}

        prev_date = self.get_prev_day_datetime(dt)
        if not prev_date:
            return put_ivs, call_ivs

        day_bars = self.bars.get(prev_date, {})

        for call in chain.calls.values():
            bar = day_bars.get(call.vt_symbol)
            if bar and hasattr(bar, "iv"):
                call_ivs[call.strike_price] = bar.iv

        for put in chain.puts.values():
            bar = day_bars.get(put.vt_symbol)
            if bar and hasattr(bar, "iv"):
                put_ivs[put.strike_price] = bar.iv

        return call_ivs, put_ivs

# 前日比用のVI Data
class PreviousDayViData:
    """"""
    def __init__(self, event_engine: EventEngine) -> None:
        self.vis: dict[datetime, float] = {}
        self.sorted_dates: list[datetime] = []
        self.event_engine: EventEngine = event_engine

    def sort_vi_datetime(self) -> None:
        """"""
        self.sorted_dates = sorted(self.vis.keys(), reverse=True)

    def get_prev_day_datetime(self, dt: datetime) -> datetime | None:
        """"""
        for i, date in enumerate(self.sorted_dates):
            if date < dt:
                return date
        return None

    def add_bar(self, bar: BarData) -> None:
        day_datetime = bar.datetime
        if not day_datetime in self.vis:
            self.vis[day_datetime] = bar.close_price

    def add_vi(self, dt: datetime, vi: float) -> None:
        self.vis[dt] = vi

    def get_prev_day_vi(self, dt: datetime) -> float | None:
        """"""
        prev_date = self.get_prev_day_datetime(dt)
        if not prev_date:
            return None
        return self.vis.get(prev_date, None)

    def put_vi_event(self, vi: ViData) -> None:
        """"""
        event: Event = Event(EVENT_VI, vi)
        self.event_engine.put(event)


@lru_cache(maxsize=100)
def get_underlying_prefix(portfolio_name: str) -> str:
    """
    基于期权产品名称获取对应标的代码

    已知规则：
    "510050_O.SSE": "510050"
    "159919_O.SZSE": "159919"

    "IO.CFFEX": "IF",
    "HO.CFFEX": "IH",
    "MO.CFFEX": "IM",

    "i_o.DCE": "i",
    "cu_o.SHFE": "cu",
    "sc_o.INE": "sc",
    "SR.CZCE": "SR",
    """
    # 上交所
    if portfolio_name.endswith("SSE"):
        return portfolio_name.replace("_O.SSE", "")
    # 深交所
    elif portfolio_name.endswith("SZSE"):
        return portfolio_name.replace("_O.SZSE", "")
    # 港交所
    elif portfolio_name.endswith("SEHK"):
        return portfolio_name.replace("_O.SEHK", "")
    # 美股
    elif portfolio_name.endswith("SMART"):
        return portfolio_name.replace("_O.SMART", "")
    # 中金所（特殊规则）
    elif portfolio_name.endswith("CFFEX"):
        d: dict = {
            "IO.CFFEX": "IF",
            "HO.CFFEX": "IH",
            "MO.CFFEX": "IM",
        }
        prefix: str = d.get(portfolio_name, "")
        return prefix
    # 上期所
    elif portfolio_name.endswith("SHFE"):
        return portfolio_name.replace("_o.SHFE", "")
    # 能交所
    elif portfolio_name.endswith("INE"):
        return portfolio_name.replace("_o.INE", "")
    # 大商所
    elif portfolio_name.endswith("DCE"):
        return portfolio_name.replace("_o.DCE", "")
    # 郑商所
    elif portfolio_name.endswith("CZCE"):
        return portfolio_name.replace(".CZCE", "")
    # JPX
    elif portfolio_name.endswith("JPX"):
        return portfolio_name.replace("_o.JPX", "")
    # 其他
    else:
        return ""
