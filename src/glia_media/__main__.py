import argparse
import sys
from pathlib import Path

from glia_media.convert import ConvertError, convert
from glia_media.version import VersionError, publish_version, use_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="glia-media",
        description="MP4를 로컬 720p HLS로 변환합니다. 원본 파일은 변경하지 않습니다.",
    )
    parser.add_argument("input", nargs="?", type=Path, help="원본 MP4 파일")
    parser.add_argument("--output", type=Path, help="비어 있는 결과 폴더")
    parser.add_argument("--library", type=Path, help="버전을 보관할 폴더")
    parser.add_argument("--asset", help="이어서 버전을 추가할 영상 번호")
    parser.add_argument("--use-version", help="현재 보여줄 버전 번호")
    args = parser.parse_args(argv)
    try:
        if args.use_version:
            if args.input is not None or args.output is not None:
                raise VersionError("버전을 되돌릴 때는 영상 파일과 결과 폴더를 넣지 않습니다.")
            if args.library is None or args.asset is None:
                raise VersionError("버전을 되돌릴 때는 보관 폴더와 영상 번호가 필요합니다.")
            record = use_version(args.library, args.asset, args.use_version)
            print("현재 버전을 바꿨습니다.")
        elif args.library is not None:
            if args.input is None or args.output is not None:
                raise VersionError("보관 폴더로 변환할 때는 원본 MP4만 지정합니다.")
            record = publish_version(args.input, args.library, args.asset)
            print("버전을 만들었습니다.")
        else:
            if args.input is None or args.output is None or args.asset is not None:
                raise VersionError("원본 MP4와 비어 있는 결과 폴더를 지정합니다.")
            result = convert(args.input, args.output)
            print("변환이 끝났습니다.")
            print(f"결과: {result.playlist}")
            print(f"화면: {result.width}x{result.height}")
            print(f"길이: {result.duration_seconds:.1f}초")
            print(f"조각: {result.segment_count}개")
            return 0
    except (ConvertError, VersionError) as error:
        summary = getattr(error, "summary", str(error))
        print(summary, file=sys.stderr)
        record_path = getattr(error, "record", None)
        detail = getattr(error, "detail", "")
        if record_path is not None:
            print(f"기록: {record_path}", file=sys.stderr)
        elif detail:
            print(detail, file=sys.stderr)
        return 1

    print(f"영상: {record.asset_id}")
    print(f"버전: {record.version_id}")
    print(f"번호: {record.number}")
    print(f"결과: {record.playlist}")
    print(f"화면: {record.width}x{record.height}")
    print(f"길이: {record.duration_seconds:.1f}초")
    print(f"조각: {record.segment_count}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
