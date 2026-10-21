"""NOL 티켓 예매 보조 프로그램 진입점: python main.py"""
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 이상이 필요합니다.")

from nolassist.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
