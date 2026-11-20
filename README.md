# glia-media

GLIA의 독립 미디어 처리기입니다. MP4 하나를 로컬에서 720p HLS로 변환하고, 비공개 R2의 `vod/` 아래에 올린 뒤, 짧은 재생 권한이 있는 요청만 Cloudflare Worker가 읽습니다.

LMS 데이터베이스는 열지 않습니다. 수강 여부는 LMS가 판단하고, 이 저장소는 변환과 비공개 재생만 담당합니다.

## 이 버전이 하지 않는 일

- 화질은 720p 하나입니다. 네트워크에 맞춰 화질을 바꾸지 않습니다.
- 재생 주소에 들어 있는 권한이 유효하면 그 시간 동안 해당 버전의 재생 목록과 조각을 읽을 수 있습니다. 수강 명단은 Worker가 보지 않습니다.
- 이미 올라간 객체는 크기가 같으면 같은 파일로 보고 건너뜁니다. 크기가 같고 내용이 다른 경우는 구분하지 않습니다.
- 작업 줄은 보관 폴더의 SQLite 하나입니다. 컴퓨터 여러 대가 같은 줄을 나누어 처리하지 않습니다.
- 변환 요청의 원본은 이 컴퓨터의 MP4 경로입니다.

## 준비

- Python 3.13
- FFmpeg와 ffprobe
- HLS 전용 R2 버킷. 버킷 이름은 `online-glia`가 아니어야 합니다.
- Worker를 배포할 때는 Node.js와 Wrangler가 필요합니다. 테스트만 돌릴 때는 Wrangler 로그인이 필요 없습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 설정

`.env.example`을 `.env`로 복사합니다. `.env`는 git에 올라가지 않습니다.

| 이름 | 의미 |
| --- | --- |
| `GLIA_R2_ENDPOINT` | `https://<계정>.r2.cloudflarestorage.com` |
| `GLIA_R2_BUCKET` | HLS 전용 버킷. `online-glia`는 거부됩니다. |
| `GLIA_R2_ACCESS_KEY_ID` | 그 버킷만 읽고 쓰는 키 |
| `GLIA_R2_SECRET_ACCESS_KEY` | 그 키의 비밀 |
| `GLIA_LMS_TOKEN` | LMS가 이 API를 부를 때 보내는 Bearer 토큰 |
| `GLIA_LMS_CALLBACK_URL` | 작업이 끝났을 때 알릴 주소. 비워 둘 수 있습니다. |
| `GLIA_LISTEN` | API 주소. 기본값은 `127.0.0.1:8765` |
| `GLIA_PLAYBACK_BASE_URL` | 재생 Worker의 `https://` 주소 |

Worker의 `PLAYBACK_SECRET`은 `GLIA_LMS_TOKEN`과 같아야 합니다. `workers/playback/.dev.vars.example`을 `.dev.vars`로 복사해 로컬에서만 넣고, Wrangler에는 `wrangler secret put PLAYBACK_SECRET`으로 넣습니다. 비밀은 `wrangler.toml`에 적지 않습니다.

## 실행

```bash
# MP4 하나를 로컬 HLS로 변환합니다. 원본 파일은 바꾸지 않습니다.
glia-media 원본.mp4 --output 결과폴더

# 버전으로 보관합니다.
glia-media 원본.mp4 --library 보관폴더

# 현재 버전을 비공개 버킷 vod/ 로 올립니다.
glia-media --library 보관폴더 --asset <영상번호> --upload

# 변환과 업로드를 작업으로 남기고 처리합니다.
glia-media 원본.mp4 --library 보관폴더 --job

# 기한이 된 작업을 한 번 처리합니다.
glia-media --library 보관폴더 --worker

# LMS가 호출하는 API를 엽니다.
glia-media --library 보관폴더 --serve
```

`POST /v1/playback`는 재생 권한과 `playlist_url`을 돌려줍니다. 주소 형태는 다음과 같습니다.

`https://<재생호스트>/p/<권한>/vod/packages/<영상>/<버전>/720p/index.m3u8`

재생 목록 안의 `segment-00000.ts`는 같은 권한 경로에서 요청됩니다. 원본 MP4와 `vod/` 밖의 키는 거절됩니다. 권한 시간은 영상 길이에 30분을 더한 값이고, 최소 10분, 최대 8시간입니다.

## 테스트

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
node --test workers/playback/test/playback.test.js
```

## Worker 배포

```bash
cd workers/playback
npx wrangler login
npx wrangler secret put PLAYBACK_SECRET
npx wrangler deploy
```

배포된 주소는 `GLIA_PLAYBACK_BASE_URL`과 같아야 합니다.
