"""
Launch VeighNa Trader with the CTP test gateway (SimNow评测环境).

NOTE: vnpy_ctptest bundles CTP 6.7.2 libraries, which are incompatible with
vnpy_ctp's (6.7.11) in the same process, so it must run in its own process —
do not import vnpy_ctp here.
"""

from vnpy.event import EventEngine

from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_ctptest import CtptestGateway

from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_datamanager import DataManagerApp


def main():
    """
    Create the main window with the CTP test gateway and common apps.
    """
    qapp = create_qapp()

    event_engine = EventEngine()

    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(CtptestGateway)

    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(DataManagerApp)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
