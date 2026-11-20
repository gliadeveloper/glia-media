import argparse
import sys
from pathlib import Path

from glia_media.convert import ConvertError, convert
from glia_media.jobs import Job, JobError, request_job, run_job, run_pending
from glia_media.lms import LmsError, job_view, listen_address, lms_settings, notify_lms
from glia_media.r2_config import ConfigError, load_r2_config
from glia_media.r2_upload import UploadError, upload_version
from glia_media.serve import serve
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
    parser.add_argument("--version", help="올릴 버전 번호. 없으면 현재 버전")
    parser.add_argument("--use-version", help="현재 보여줄 버전 번호")
    parser.add_argument("--upload", action="store_true", help="HLS 전용 버킷의 vod/ 로 올린다")
    parser.add_argument("--job", action="store_true", help="변환과 업로드를 작업 기록으로 남긴다")
    parser.add_argument("--retry-job", help="실패한 작업 번호")
    parser.add_argument("--worker", action="store_true", help="기한이 된 작업을 순서대로 처리한다")
    parser.add_argument("--serve", action="store_true", help="LMS가 변환을 요청하는 API를 연다")
    args = parser.parse_args(argv)
    try:
        if args.serve:
            if (
                args.input is not None
                or args.output is not None
                or args.asset is not None
                or args.version is not None
                or args.use_version is not None
                or args.upload
                or args.job
                or args.retry_job is not None
                or args.worker
            ):
                raise LmsError("API를 열 때는 보관 폴더만 지정합니다.")
            if args.library is None:
                raise LmsError("API를 열 때는 보관 폴더가 필요합니다.")
            settings = lms_settings(env_file=Path.cwd() / ".env")
            if not settings["token"]:
                raise LmsError("GLIA_LMS_TOKEN 이 필요합니다.")
            if not settings["playback_base_url"]:
                raise LmsError("GLIA_PLAYBACK_BASE_URL 이 필요합니다.")
            host, port = listen_address(settings["listen"])
            print(f"LMS 연결을 열었습니다. {host}:{port}")
            serve(args.library, settings["token"], host, port, playback_base=settings["playback_base_url"])
            return 0
        if args.worker:
            if (
                args.input is not None
                or args.output is not None
                or args.asset is not None
                or args.version is not None
                or args.use_version is not None
                or args.upload
                or args.job
                or args.retry_job is not None
            ):
                raise JobError("워커는 보관 폴더만 지정합니다.")
            if args.library is None:
                raise JobError("워커에는 보관 폴더가 필요합니다.")
            settings = lms_settings(env_file=Path.cwd() / ".env")

            def tell_lms(job: Job) -> None:
                if not settings["token"] or not settings["callback_url"]:
                    return
                if job.state not in {"READY", "FAILED"}:
                    return
                try:
                    notify_lms(settings["callback_url"], settings["token"], job_view(args.library, job))
                except LmsError as error:
                    print(error.summary, file=sys.stderr)

            finished = run_pending(
                args.library,
                env_file=Path.cwd() / ".env",
                on_finished=tell_lms,
            )
            if not finished:
                print("처리할 작업이 없습니다.")
                return 0
            failed = False
            for job in finished:
                print(f"작업: {job.id}")
                print(f"상태: {job.state}")
                if job.state != "READY":
                    failed = True
                    if job.last_error:
                        print(job.last_error, file=sys.stderr)
            return 1 if failed else 0
        if args.job or args.retry_job:
            if args.output is not None or args.use_version is not None or args.upload or args.version is not None:
                raise JobError("작업 기록에는 보관 폴더와 원본 MP4만 지정합니다.")
            if args.library is None:
                raise JobError("작업 기록에는 보관 폴더가 필요합니다.")
            if args.retry_job:
                if args.input is not None or args.job:
                    raise JobError("작업을 다시 할 때는 작업 번호와 보관 폴더만 지정합니다.")
                job_id = args.retry_job
            else:
                if args.input is None:
                    raise JobError("작업으로 남길 원본 MP4가 필요합니다.")
                job_id = request_job(args.library, args.input, args.asset).id
            try:
                job = run_job(args.library, job_id, env_file=Path.cwd() / ".env")
            except JobError as error:
                print(error.summary, file=sys.stderr)
                print(f"작업: {job_id}", file=sys.stderr)
                return 1
            print("작업을 기록했습니다.")
            print(f"작업: {job.id}")
            print(f"상태: {job.state}")
            if job.asset_id:
                print(f"영상: {job.asset_id}")
            if job.version_id:
                print(f"버전: {job.version_id}")
            if job.last_error:
                print(f"오류: {job.last_error}")
            return 0
        if args.upload:
            if args.input is not None or args.output is not None or args.use_version is not None:
                raise UploadError("올릴 때는 보관 폴더와 영상 번호만 지정합니다.")
            if args.library is None or args.asset is None:
                raise UploadError("올릴 때는 보관 폴더와 영상 번호가 필요합니다.")
            result = upload_version(
                args.library,
                args.asset,
                args.version,
                load_r2_config(env_file=Path.cwd() / ".env"),
            )
            print("업로드가 끝났습니다.")
            print(f"버킷: {result.bucket}")
            print(f"새로 올림: {len(result.uploaded)}개")
            print(f"이미 있음: {len(result.already_present)}개")
            return 0
        if args.use_version:
            if args.input is not None or args.output is not None or args.version is not None:
                raise VersionError("버전을 되돌릴 때는 영상 파일과 결과 폴더를 넣지 않습니다.")
            if args.library is None or args.asset is None:
                raise VersionError("버전을 되돌릴 때는 보관 폴더와 영상 번호가 필요합니다.")
            record = use_version(args.library, args.asset, args.use_version)
            print("현재 버전을 바꿨습니다.")
        elif args.library is not None:
            if args.input is None or args.output is not None or args.version is not None or args.retry_job is not None:
                raise VersionError("보관 폴더로 변환할 때는 원본 MP4만 지정합니다.")
            record = publish_version(args.input, args.library, args.asset)
            print("버전을 만들었습니다.")
        else:
            if args.input is None or args.output is None or args.asset is not None or args.version is not None:
                raise VersionError("원본 MP4와 비어 있는 결과 폴더를 지정합니다.")
            result = convert(args.input, args.output)
            print("변환이 끝났습니다.")
            print(f"결과: {result.playlist}")
            print(f"화면: {result.width}x{result.height}")
            print(f"길이: {result.duration_seconds:.1f}초")
            print(f"조각: {result.segment_count}개")
            return 0
    except (ConvertError, VersionError, UploadError, ConfigError, JobError, LmsError) as error:
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
