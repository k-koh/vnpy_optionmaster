from datetime import datetime
import pyqtgraph as pg
from typing import cast

from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.database import DB_TZ

from ..base import PortfolioData, OptionData, PreviousDayOptionData
from ..engine import OptionEngine, Event, EventEngine
from ..time import ANNUAL_DAYS

import numpy as np
import matplotlib
matplotlib.use('Qt5Agg')                    # noqa
import matplotlib.pyplot as plt             # noqa
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas  # noqa
from matplotlib.figure import Figure        # noqa
from mpl_toolkits.mplot3d import Axes3D     # noqa
from pylab import mpl                       # noqa

plt.style.use("dark_background")
mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei']   # set font for Chinese
mpl.rcParams['axes.unicode_minus'] = False


class OptionVolatilityChart(QtWidgets.QWidget):

    signal_timer: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        """"""
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.event_engine: EventEngine = option_engine.event_engine
        self.portfolio_name: str = portfolio_name

        self.timer_count: int = 0
        self.timer_trigger: int = 3

        self.chain_checks: dict[str, QtWidgets.QCheckBox] = {}
        self.put_bid_curves: dict[str, pg.PlotCurveItem] = {}
        self.put_ask_curves: dict[str, pg.PlotCurveItem] = {}
        self.put_mid_curves: dict[str, pg.PlotCurveItem] = {}

        self.call_bid_curves: dict[str, pg.PlotCurveItem] = {}
        self.call_ask_curves: dict[str, pg.PlotCurveItem] = {}
        self.call_mid_curves: dict[str, pg.PlotCurveItem] = {}
        self.pricing_curves: dict[str, pg.PlotCurveItem] = {}
        self.eris_p_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.eris_c_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.atm_strike_lines: dict[str, pg.InfiniteLine] = {}
        self.total_volume_bars: dict[str, pg.BarGraphItem] = {}

        self.prev_call_curves: dict[str, pg.PlotCurveItem] = {}
        self.prev_put_curves: dict[str, pg.PlotCurveItem] = {}

        self.iv_diff_pos_bars: dict[str, pg.BarGraphItem] = {}
        self.iv_diff_neg_bars: dict[str, pg.BarGraphItem] = {}
        self.iv_diff_pos_text_items: dict[str, list[pg.TextItem]] = {} # Added for IV diff text
        self.iv_diff_neg_text_items: dict[str, list[pg.TextItem]] = {} # Added for IV diff text
        self.total_volume_text_items: dict[str, list[pg.TextItem]] = {} # Added for Volume text
        self.chain_colors: dict[str, tuple] = {} # Added to store color for each chain

        self.colors: list = [
            (255, 0, 0),
            (255, 255, 0),
            (0, 255, 0),
            (0, 0, 255),
            (0, 128, 0),
            (19, 234, 201),
            (195, 46, 212),
            (250, 194, 5),
            (0, 114, 189),
        ]

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle("波动率曲线")

        # Create checkbox for each chain
        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)

        chain_symbols: list = list(portfolio.chains.keys())
        chain_symbols.sort()

        hbox.addStretch()

        for chain_symbol in chain_symbols:
            chain_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
            chain_check.setText(chain_symbol.split(".")[0])
            chain_check.setChecked(True)
            chain_check.stateChanged.connect(self.update_curve_visible)

            hbox.addWidget(chain_check)
            self.chain_checks[chain_symbol] = chain_check

        hbox.addStretch()

        # Create graphics window
        pg.setConfigOptions(antialias=True)

        graphics_window: pg.GraphicsLayoutWidget = pg.GraphicsLayoutWidget()
        self.impv_chart = graphics_window.addPlot(row=0, col=0, title="隐含波动率曲线")
        self.impv_chart.showGrid(x=True, y=True)
        self.impv_chart.setLabel("left", "波动率")
        self.impv_chart.setLabel("bottom", "行权价")
        self.impv_chart.addLegend()
        self.impv_chart.setMenuEnabled(False)
        self.impv_chart.setMouseEnabled(False, False)

        graphics_window.nextRow()

        self.volume_chart = graphics_window.addPlot(row=1, col=0, title="成交量")
        self.volume_chart.showGrid(x=True, y=True)
        self.volume_chart.setLabel("left", "成交量")
        self.volume_chart.setLabel("bottom", "行权价")
        self.volume_chart.setXLink(self.impv_chart)
        self.volume_chart.addLegend()
        self.volume_chart.setMenuEnabled(False)
        self.volume_chart.setMouseEnabled(False, False)
        self.volume_chart.setMaximumHeight(200)

        graphics_window.nextRow()

        self.iv_diff_chart = graphics_window.addPlot(row=2, col=0, title="前日比IV")
        self.iv_diff_chart.showGrid(x=True, y=True)
        self.iv_diff_chart.setLabel("left", "IV差值")
        self.iv_diff_chart.setLabel("bottom", "行权价")
        self.iv_diff_chart.setXLink(self.impv_chart)
        self.iv_diff_chart.addLegend()
        self.iv_diff_chart.setMenuEnabled(False)
        self.iv_diff_chart.setMouseEnabled(False, False)
        self.iv_diff_chart.setMaximumHeight(200)

        for chain_symbol in chain_symbols:
            self.add_impv_curve(chain_symbol)

        # Set Layout
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(graphics_window)
        self.setLayout(vbox)

    def register_event(self) -> None:
        """"""
        self.signal_timer.connect(self.process_timer_event)

        self.event_engine.register(EVENT_TIMER, self.signal_timer.emit)

    def process_timer_event(self, event: Event) -> None:
        """"""
        self.timer_count += 1
        if self.timer_count < self.timer_trigger:
            return
        self.timer_trigger = 0

        self.update_curve_data()
        self.update_curve_visible() # Ensure visibility is updated for newly created items

    def add_impv_curve(self, chain_symbol: str) -> None:
        """"""
        symbol_size: int = 14
        symbol: str = chain_symbol.split(".")[0]
        color: tuple = self.colors.pop(0)
        self.chain_colors[chain_symbol] = color # Store the color for this chain
        pen: QtGui.QPen = pg.mkPen(color, width=2)
        pen_dot: QtGui.QPen = pg.mkPen(color, style=QtCore.Qt.DotLine)
        pen_prev_day: QtGui.QPen = pg.mkPen(color, style=QtCore.Qt.DashLine)

        self.call_mid_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " 看涨",
            pen=pen,
        )
        self.put_mid_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " 看跌",
            pen=pen,
        )

        self.call_bid_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t1",
            name=symbol + " 看涨买",
            symbolBrush=color
        )
        self.call_ask_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t1",
            name=symbol + " 看涨卖",
            symbolBrush=color
        )
        self.put_bid_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t",
            name=symbol + " 看跌买",
            symbolBrush=color
        )
        self.put_ask_curves[chain_symbol] = self.impv_chart.plot(
            pen=None,
            symbolSize=symbol_size,
            symbol="t",
            name=symbol + " 看跌卖",
            symbolBrush=color
        )

        self.pricing_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=symbol_size,
            symbol="o",
            name=symbol + " 定价",
            pen=pen_dot,
            symbolBrush=color
        )

        self.prev_call_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " 前日看涨",
            pen=pen_prev_day,
        )
        self.prev_put_curves[chain_symbol] = self.impv_chart.plot(
            symbolSize=0,
            name=symbol + " 前日看跌",
            pen=pen_prev_day,
        )

        p_line_pen = pg.mkPen(color=color, width=2, style=QtCore.Qt.DotLine)
        c_line_pen = pg.mkPen(color=color, width=2, style=QtCore.Qt.DotLine)
        atm_line_pen = pg.mkPen(color=(255, 174, 201), width=2, style=QtCore.Qt.DotLine)

        self.eris_p_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=p_line_pen,
            label=symbol + " 看跌ERIS",
            labelOpts={'position': 0.95, 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )
        self.eris_c_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=c_line_pen,
            label=symbol + " 看涨ERIS",
            labelOpts={'position': 0.05, 'color': color, 'fill': (200,200,200,50), 'movable': False}
        )
        self.atm_strike_lines[chain_symbol] = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=atm_line_pen,
            label=symbol + " ATM",
            labelOpts={'position': 0.5, 'color': (255, 174, 201), 'fill': (200,200,200,50), 'movable': False}
        )

        self.impv_chart.addItem(self.eris_p_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.eris_c_strike_lines[chain_symbol])
        self.impv_chart.addItem(self.atm_strike_lines[chain_symbol])

        self.eris_p_strike_lines[chain_symbol].hide()
        self.eris_c_strike_lines[chain_symbol].hide()
        self.atm_strike_lines[chain_symbol].hide()

        self.total_volume_bars[chain_symbol] = pg.BarGraphItem(
            x=[],
            height=[],
            width=1.0,
            brush=pg.mkBrush(color=color), # Use a neutral color for combined volume
            name=symbol + " 总成交量"
        )
        self.volume_chart.addItem(self.total_volume_bars[chain_symbol])

        self.iv_diff_pos_bars[chain_symbol] = pg.BarGraphItem(
            x=[],
            height=[],
            width=1.0,
            brush=pg.mkBrush(color=color),
            name=symbol + " IV diff pos"
        )
        self.iv_diff_neg_bars[chain_symbol] = pg.BarGraphItem(
            x=[],
            height=[],
            width=1.0,
            brush=pg.mkBrush(color=color + (100,)),
            name=symbol + " IV diff neg"
        )
        self.iv_diff_chart.addItem(self.iv_diff_pos_bars[chain_symbol])
        self.iv_diff_chart.addItem(self.iv_diff_neg_bars[chain_symbol])

        self.iv_diff_pos_text_items[chain_symbol] = [] # Initialize list for text items
        self.iv_diff_neg_text_items[chain_symbol] = []
        self.total_volume_text_items[chain_symbol] = []

    def update_curve_data(self) -> None:
        """"""
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)
        prev_day_data: PreviousDayOptionData = self.option_engine.prev_day_option

        for chain in portfolio.chains.values():
            # Clear previous text items for IV diff chart
            for text_item in self.iv_diff_pos_text_items[chain.chain_symbol]:
                self.iv_diff_chart.removeItem(text_item)
            self.iv_diff_pos_text_items[chain.chain_symbol].clear()

            for text_item in self.iv_diff_neg_text_items[chain.chain_symbol]:
                self.iv_diff_chart.removeItem(text_item)
            self.iv_diff_neg_text_items[chain.chain_symbol].clear()

            # Clear previous text items for Volume chart
            for text_item in self.total_volume_text_items[chain.chain_symbol]:
                self.volume_chart.removeItem(text_item)
            self.total_volume_text_items[chain.chain_symbol].clear()

            # Get call data
            call_mid_impv: list = []
            call_bid_impv: list = []
            call_ask_impv: list = []
            pricing_impv: list = []
            call_strikes: list = []
            call_volumes: list = []

            calls: list[OptionData] = list(chain.calls.values())
            calls.sort(key=lambda x: x.strike_price)

            for call in calls:
                call_mid_impv.append(call.mid_impv * 100)
                call_bid_impv.append(call.bid_impv * 100)
                call_ask_impv.append(call.ask_impv * 100)
                pricing_impv.append(call.pricing_impv * 100)
                call_strikes.append(call.strike_price)

                if call.tick:
                    call_volumes.append(call.tick.volume)
                else:
                    call_volumes.append(0)

            # Get put data
            put_mid_impv: list = []
            put_bid_impv: list = []
            put_ask_impv: list = []
            put_strikes: list = []
            put_volumes: list = []

            puts: list[OptionData] = list(chain.puts.values())
            puts.sort(key=lambda x: x.strike_price)

            for put in puts:
                put_mid_impv.append(put.mid_impv * 100)
                put_bid_impv.append(put.bid_impv * 100)
                put_ask_impv.append(put.ask_impv * 100)
                put_strikes.append(put.strike_price)

                if put.tick:
                    put_volumes.append(put.tick.volume)
                else:
                    put_volumes.append(0)

            # Get previous day iv
            prev_call_ivs: list = []
            prev_put_ivs: list = []
            if prev_day_data:
                dt: datetime = datetime.now(DB_TZ)

                prev_call_data, prev_put_data = prev_day_data.get_prev_day_iv_curve(dt, chain)

                for strike in call_strikes:
                    iv = prev_call_data.get(strike, 0)
                    prev_call_ivs.append(iv * 100)

                for strike in put_strikes:
                    iv = prev_put_data.get(strike, 0)
                    prev_put_ivs.append(iv * 100)

            # Calculate IV difference
            iv_diff_strikes = []
            iv_diff_heights = []

            all_strikes = sorted(list(set(call_strikes + put_strikes)))
            prev_call_iv_map = {s: v for s, v in zip(call_strikes, prev_call_ivs)}
            prev_put_iv_map = {s: v for s, v in zip(put_strikes, prev_put_ivs)}
            call_mid_impv_map = {s: v for s, v in zip(call_strikes, call_mid_impv)}
            put_mid_impv_map = {s: v for s, v in zip(put_strikes, put_mid_impv)}

            for strike in all_strikes:
                call_iv = call_mid_impv_map.get(strike, 0)
                put_iv = put_mid_impv_map.get(strike, 0)
                prev_call_iv = prev_call_iv_map.get(strike, 0)
                prev_put_iv = prev_put_iv_map.get(strike, 0)

                has_call = call_iv > 0
                has_put = put_iv > 0

                current_iv = 0
                prev_iv = 0

                if has_call and has_put:
                    current_iv = (call_iv + put_iv) / 2.0

                    p_count = 0
                    p_sum = 0
                    if prev_call_iv > 0:
                        p_sum += prev_call_iv
                        p_count += 1
                    if prev_put_iv > 0:
                        p_sum += prev_put_iv
                        p_count += 1
                    if p_count > 0:
                        prev_iv = p_sum / p_count

                elif has_call:
                    current_iv = call_iv
                    prev_iv = prev_call_iv
                elif has_put:
                    current_iv = put_iv
                    prev_iv = prev_put_iv
                else:
                    continue

                if prev_iv != 0:
                    iv_diff_strikes.append(strike)
                    iv_diff_heights.append(current_iv - prev_iv)

            iv_diff_data = list(zip(iv_diff_strikes, iv_diff_heights))
            iv_diff_data.sort(key=lambda item: abs(item[1]), reverse=True)

            pos_strikes = []
            pos_heights = []
            neg_strikes = []
            neg_heights = []

            for strike, height in iv_diff_data:
                if height >= 0:
                    pos_strikes.append(strike)
                    pos_heights.append(height)
                else:
                    neg_strikes.append(strike)
                    neg_heights.append(height)

            # Plot curves
            self.call_mid_curves[chain.chain_symbol].setData(
                y=call_mid_impv,
                x=call_strikes
            )
            self.call_bid_curves[chain.chain_symbol].setData(
                y=call_bid_impv,
                x=call_strikes
            )
            self.call_ask_curves[chain.chain_symbol].setData(
                y=call_ask_impv,
                x=call_strikes
            )
            self.put_mid_curves[chain.chain_symbol].setData(
                y=put_mid_impv,
                x=put_strikes
            )
            self.put_bid_curves[chain.chain_symbol].setData(
                y=put_bid_impv,
                x=put_strikes
            )
            self.put_ask_curves[chain.chain_symbol].setData(
                y=put_ask_impv,
                x=put_strikes
            )
            self.pricing_curves[chain.chain_symbol].setData(
                y=pricing_impv,
                x=call_strikes
            )

            if prev_call_ivs and prev_put_ivs:
                self.prev_call_curves[chain.chain_symbol].setData(
                    y=prev_call_ivs,
                    x=call_strikes
                )
                self.prev_put_curves[chain.chain_symbol].setData(
                    y=prev_put_ivs,
                    x=put_strikes
                )

            # Update ERIS strike lines
            if chain.eris_p_strike is not None:
                self.eris_p_strike_lines[chain.chain_symbol].setPos(chain.eris_p_strike)
                self.eris_p_strike_lines[chain.chain_symbol].show()
            else:
                self.eris_p_strike_lines[chain.chain_symbol].hide()

            if chain.eris_c_strike is not None:
                self.eris_c_strike_lines[chain.chain_symbol].setPos(chain.eris_c_strike)
                self.eris_c_strike_lines[chain.chain_symbol].show()
            else:
                self.eris_c_strike_lines[chain.chain_symbol].hide()

            # Update ATM strike line
            if chain.atm_price:
                self.atm_strike_lines[chain.chain_symbol].setPos(chain.atm_price)
                self.atm_strike_lines[chain.chain_symbol].show()
            else:
                self.atm_strike_lines[chain.chain_symbol].hide()

            # Calculate strike_step
            strike_step = 0
            if len(all_strikes) > 1:
                all_strikes.sort() # Ensure sorted to correctly calculate step
                strike_step = all_strikes[1] - all_strikes[0]

            # Update volume bars
            volume_strikes = sorted(list(set(call_strikes + put_strikes)))
            call_volume_map = {s: v for s, v in zip(call_strikes, call_volumes)}
            put_volume_map = {s: v for s, v in zip(put_strikes, put_volumes)}

            total_volumes = []
            for s in volume_strikes:
                total_vol = call_volume_map.get(s, 0) + put_volume_map.get(s, 0)
                total_volumes.append(total_vol)

            volume_data = list(zip(volume_strikes, total_volumes))
            volume_data.sort(key=lambda item: item[1], reverse=True)
            sorted_volume_strikes = [item[0] for item in volume_data]
            sorted_total_volumes = [item[1] for item in volume_data]

            bar_width = strike_step * 0.5 if strike_step else 100

            self.total_volume_bars[chain.chain_symbol].setOpts(
                x=sorted_volume_strikes, height=sorted_total_volumes, width=bar_width
            )

            # Add text labels for Volume bars
            chain_color = self.chain_colors[chain.chain_symbol]
            font = QtGui.QFont()
            font.setPointSize(8)

            # Calculate dynamic offset based on the maximum volume
            if sorted_total_volumes:
                max_volume = max(sorted_total_volumes)
                # Use 10% of the max volume as offset, with a minimum of 10
                volume_text_offset = max(max_volume * 0.1, 20)
            else:
                volume_text_offset = 20 # Default offset

            for strike, volume in zip(sorted_volume_strikes, sorted_total_volumes):
                if volume == 0:
                    continue
                text_item = pg.TextItem(
                    text=f"{volume:.0f}",
                    color=chain_color,
                    anchor=(0.5, 0)
                )
                text_item.setFont(font)
                text_item.setPos(strike, volume + volume_text_offset) # Position with dynamic offset
                self.volume_chart.addItem(text_item)
                self.total_volume_text_items[chain.chain_symbol].append(text_item)

            bar_width_diff = bar_width
            self.iv_diff_pos_bars[chain.chain_symbol].setOpts(x=pos_strikes, height=pos_heights, width=bar_width_diff)
            self.iv_diff_neg_bars[chain.chain_symbol].setOpts(x=neg_strikes, height=neg_heights, width=bar_width_diff)

            # Add text labels for IV diff bars
            chain_color = self.chain_colors[chain.chain_symbol]
            font = QtGui.QFont()
            font.setPointSize(8) # Smaller font size for better fit
            IV_TEXT_OFFSET = 0.5 # Offset for text above/below the bar

            for strike, height in zip(pos_strikes, pos_heights):
                text_item = pg.TextItem(
                    text=f"{height:.2f}%",
                    color=chain_color,
                    anchor=(0.5, 0) # Center above the bar
                )
                text_item.setFont(font)
                text_item.setPos(strike, height + IV_TEXT_OFFSET)
                self.iv_diff_chart.addItem(text_item)
                self.iv_diff_pos_text_items[chain.chain_symbol].append(text_item)

            for strike, height in zip(neg_strikes, neg_heights):
                text_item = pg.TextItem(
                    text=f"{height:.2f}%",
                    color=chain_color,
                    anchor=(0.5, 1) # Center below the bar
                )
                text_item.setFont(font)
                text_item.setPos(strike, height - IV_TEXT_OFFSET)
                self.iv_diff_chart.addItem(text_item)
                self.iv_diff_neg_text_items[chain.chain_symbol].append(text_item)

    def update_curve_visible(self) -> None:
        """"""
        for chain_symbol, checkbox in self.chain_checks.items():
            call_mid_curve: pg.PlotCurveItem = self.call_mid_curves[chain_symbol]
            call_bid_curve: pg.PlotCurveItem = self.call_bid_curves[chain_symbol]
            call_ask_curve: pg.PlotCurveItem = self.call_ask_curves[chain_symbol]
            put_mid_curve: pg.PlotCurveItem = self.put_mid_curves[chain_symbol]
            put_bid_curve: pg.PlotCurveItem = self.put_bid_curves[chain_symbol]
            put_ask_curve: pg.PlotCurveItem = self.put_ask_curves[chain_symbol]
            pricing_curve: pg.PlotCurveItem = self.pricing_curves[chain_symbol]
            prev_call_curve: pg.PlotCurveItem = self.prev_call_curves[chain_symbol]
            prev_put_curve: pg.PlotCurveItem = self.prev_put_curves[chain_symbol]
            p_line = self.eris_p_strike_lines[chain_symbol]
            c_line = self.eris_c_strike_lines[chain_symbol]
            atm_line = self.atm_strike_lines[chain_symbol]
            total_volume_bar = self.total_volume_bars[chain_symbol]
            iv_diff_pos_bar = self.iv_diff_pos_bars[chain_symbol]
            iv_diff_neg_bar = self.iv_diff_neg_bars[chain_symbol]

            if checkbox.isChecked():
                call_mid_curve.show()
                call_bid_curve.show()
                call_ask_curve.show()
                put_mid_curve.show()
                put_bid_curve.show()
                put_ask_curve.show()
                pricing_curve.show()
                prev_call_curve.show()
                prev_put_curve.show()
                total_volume_bar.show()
                iv_diff_pos_bar.show()
                iv_diff_neg_bar.show()
                for text_item in self.iv_diff_pos_text_items[chain_symbol]:
                    text_item.show()
                for text_item in self.iv_diff_neg_text_items[chain_symbol]:
                    text_item.show()
                for text_item in self.total_volume_text_items[chain_symbol]:
                    text_item.show()
            else:
                call_mid_curve.hide()
                call_bid_curve.hide()
                call_ask_curve.hide()
                put_mid_curve.hide()
                put_bid_curve.hide()
                put_ask_curve.hide()
                pricing_curve.hide()
                prev_call_curve.hide()
                prev_put_curve.hide()
                p_line.hide()
                c_line.hide()
                atm_line.hide()
                total_volume_bar.hide()
                iv_diff_pos_bar.hide()
                iv_diff_neg_bar.hide()
                for text_item in self.iv_diff_pos_text_items[chain_symbol]:
                    text_item.hide()
                for text_item in self.iv_diff_neg_text_items[chain_symbol]:
                    text_item.hide()
                for text_item in self.total_volume_text_items[chain_symbol]:
                    text_item.hide()


class ScenarioAnalysisChart(QtWidgets.QWidget):
    """"""

    def __init__(self, option_engine: OptionEngine, portfolio_name: str) -> None:
        """"""
        super().__init__()

        self.option_engine: OptionEngine = option_engine
        self.portfolio_name: str = portfolio_name

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle("情景分析")

        # Create widgets
        self.price_change_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.price_change_spin.setSuffix("%")
        self.price_change_spin.setMinimum(2)
        self.price_change_spin.setValue(10)

        self.impv_change_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.impv_change_spin.setSuffix("%")
        self.impv_change_spin.setMinimum(2)
        self.impv_change_spin.setValue(10)

        self.time_change_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.time_change_spin.setSuffix("日")
        self.time_change_spin.setMinimum(0)
        self.time_change_spin.setValue(1)

        self.target_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.target_combo.addItems([
            "盈亏",
            "Delta",
            "Gamma",
            "Theta",
            "Vega"
        ])

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("执行分析")
        button.clicked.connect(self.run_analysis)

        # Create charts
        fig: Figure = Figure()
        canvas: FigureCanvas = FigureCanvas(fig)

        ax = fig.add_subplot(projection="3d")
        self.ax = cast(Axes3D, ax)
        self.ax.set_xlabel("价格涨跌 %")
        self.ax.set_ylabel("波动率涨跌 %")
        self.ax.set_zlabel("盈亏")

        # Set layout
        hbox1: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox1.addWidget(QtWidgets.QLabel("目标数据"))
        hbox1.addWidget(self.target_combo)
        hbox1.addWidget(QtWidgets.QLabel("时间衰减"))
        hbox1.addWidget(self.time_change_spin)
        hbox1.addStretch()

        hbox2: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox2.addWidget(QtWidgets.QLabel("价格变动"))
        hbox2.addWidget(self.price_change_spin)
        hbox2.addWidget(QtWidgets.QLabel("波动率变动"))
        hbox2.addWidget(self.impv_change_spin)
        hbox2.addStretch()
        hbox2.addWidget(button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)
        vbox.addWidget(canvas)

        self.setLayout(vbox)

    def run_analysis(self) -> None:
        """"""
        # Generate range
        portfolio: PortfolioData = self.option_engine.get_portfolio(self.portfolio_name)

        price_change_range = self.price_change_spin.value()
        price_changes = np.arange(-price_change_range, price_change_range + 1) / 100

        impv_change_range = self.impv_change_spin.value()
        impv_changes = np.arange(-impv_change_range, impv_change_range + 1) / 100

        time_change = self.time_change_spin.value() / ANNUAL_DAYS
        target_name = self.target_combo.currentText()

        # Check underlying price exists
        for underlying in portfolio.underlyings.values():
            if not underlying.mid_price:
                QtWidgets.QMessageBox.warning(
                    self,
                    "无法执行情景分析",
                    f"标的物{underlying.symbol}当前中间价为{underlying.mid_price}",
                    QtWidgets.QMessageBox.Ok
                )
                return

        # Run analysis calculation
        pnls: list = []
        deltas: list = []
        gammas: list = []
        thetas: list = []
        vegas: list = []

        for impv_change in impv_changes:
            pnl_buf: list = []
            delta_buf: list = []
            gamma_buf: list = []
            theta_buf: list = []
            vega_buf: list = []

            for price_change in price_changes:
                portfolio_pnl = 0
                portfolio_delta = 0.0
                portfolio_gamma = 0
                portfolio_theta = 0
                portfolio_vega = 0

                # Calculate underlying pnl
                for underlying in portfolio.underlyings.values():
                    if not underlying.net_pos:
                        continue

                    value = underlying.mid_price * underlying.net_pos * underlying.size
                    portfolio_pnl += value * price_change
                    portfolio_delta += value / 100

                # Calculate option pnl
                for option in portfolio.options.values():
                    if not option.net_pos:
                        continue

                    new_underlying_price = option.underlying.mid_price * (1 + price_change)
                    new_time_to_expiry = max(option.time_to_expiry - time_change, 0)
                    new_mid_impv = option.mid_impv * (1 + impv_change)

                    new_price, delta, gamma, theta, vega = option.calculate_greeks(
                        new_underlying_price,
                        option.strike_price,
                        option.interest_rate,
                        new_time_to_expiry,
                        new_mid_impv,
                        option.option_type
                    )

                    # 添加对option.tick为None的检查
                    if option.tick is None:
                        diff = 0
                    else:
                        diff = new_price - option.tick.last_price
                    multiplier = option.net_pos * option.size

                    portfolio_pnl += diff * multiplier
                    portfolio_delta += delta * multiplier
                    portfolio_gamma += gamma * multiplier
                    portfolio_theta += theta * multiplier
                    portfolio_vega += vega * multiplier

                pnl_buf.append(portfolio_pnl)
                delta_buf.append(portfolio_delta)
                gamma_buf.append(gamma_buf)
                theta_buf.append(theta_buf)
                vega_buf.append(vega_buf)

            pnls.append(pnl_buf)
            deltas.append(delta_buf)
            gammas.append(gamma_buf)
            thetas.append(theta_buf)
            vegas.append(vega_buf)

        # Plot chart
        if target_name == "盈亏":
            target_data: list = pnls
        elif target_name == "Delta":
            target_data = deltas
        elif target_name == "Gamma":
            target_data = gammas
        elif target_name == "Theta":
            target_data = thetas
        else:
            target_data = vegas

        self.update_chart(price_changes * 100, impv_changes * 100, target_data, target_name)

    def update_chart(
        self,
        price_changes: np.ndarray,
        impv_changes: np.ndarray,
        target_data: list[list[float]],
        target_name: str
    ) -> None:
        """"""
        self.ax.clear()

        price_changes, impv_changes = np.meshgrid(price_changes, impv_changes)

        self.ax.set_zlabel(target_name)
        self.ax.plot_surface(
            X=price_changes,
            Y=impv_changes,
            Z=np.array(target_data),
            rstride=1,
            cstride=1,
            cmap='coolwarm'
        )