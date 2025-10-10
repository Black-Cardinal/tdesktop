name: MacOS Packaged

on:
  push:
    branches: ["**"]
  pull_request:
    branches: ["**"]
  workflow_dispatch: {}

jobs:
  macos:
    name: macOS release DMG
    runs-on: macos-14

    env:
      GIT: "https://github.com"
      # Qt из Homebrew собран под 14 → таргетим 14.0, чтобы не было конфликтов при линковке/рантайме
      CMAKE_OSX_DEPLOYMENT_TARGET: "14.0"
      UPLOAD_ARTIFACT: "true"

    steps:
      - name: Get repo name
        run: echo "REPO_NAME=${GITHUB_REPOSITORY##*/}" >> $GITHUB_ENV

      - name: Checkout
        uses: actions/checkout@v4
        with:
          submodules: recursive
          path: ${{ env.REPO_NAME }}

      - name: Toolchain & deps
        run: |
          brew update
          brew upgrade || true

          # Базовые зависимости
          brew unlink ffmpeg || true
          brew install \
            ada-url autoconf automake boost cmake ffmpeg@6 jpeg-xl libavif libheif libvpx \
            libtool openal-soft openh264 openssl@3 opus ninja pkg-config python qt yasm xz

          # macdeployqt в PATH
          echo "/opt/homebrew/opt/qt/bin" >> $GITHUB_PATH

          # Используем SDK из Xcode на раннере
          echo "CMAKE_OSX_SYSROOT=$(xcrun --sdk macosx --show-sdk-path)" >> $GITHUB_ENV

          # Пути для CMake и pkg-config
          echo "CMAKE_PREFIX_PATH=/opt/homebrew/opt/qt:/opt/homebrew/opt/ffmpeg@6:/opt/homebrew/opt/openal-soft" >> $GITHUB_ENV
          echo "PKG_CONFIG_PATH=/opt/homebrew/opt/ffmpeg@6/lib/pkgconfig:/opt/homebrew/lib/pkgconfig:/opt/homebrew/share/pkgconfig" >> $GITHUB_ENV

          # Библиотеки/кеш
          echo "LibrariesPath=$(pwd)" >> $GITHUB_ENV

          # Версию tg_owt запомним для корректного cache key
          curl -s -o tg_owt-version.json https://api.github.com/repos/desktop-app/tg_owt/git/refs/heads/master

      - name: RNNoise (system)
        run: |
          cd "$LibrariesPath"
          git clone --depth=1 https://gitlab.xiph.org/xiph/rnnoise.git
          cd rnnoise
          ./autogen.sh
          ./configure --disable-examples --disable-doc
          make -j"$(sysctl -n hw.logicalcpu)"
          sudo make install

      - name: Cache tg_owt
        id: cache-webrtc
        uses: actions/cache@v4
        with:
          path: ${{ env.LibrariesPath }}/tg_owt
          key: ${{ runner.os }}-webrtc-${{ hashFiles('**/tg_owt-version.json') }}

      - name: Build tg_owt (WebRTC)
        if: steps.cache-webrtc.outputs.cache-hit != 'true'
        run: |
          cd "$LibrariesPath"
          git clone --depth=1 --recursive --shallow-submodules "$GIT/desktop-app/tg_owt.git"
          cd tg_owt

          # 📌 Пиним abseil на LTS 20240116.2, чтобы сигнатуры absl совпали с вызовами
          git submodule update --init --recursive --force
          pushd src/third_party/abseil-cpp
          git fetch --tags
          git checkout 20240116.2
          popd

          # Приоритет инклюдов abseil из сабмодуля (на случай системного abseil в /opt/homebrew/include)
          ABSL_SUBMODULE="$PWD/src/third_party/abseil-cpp"
          export CPATH="$ABSL_SUBMODULE:$CPATH"

          cmake -B build -GNinja . \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_OSX_DEPLOYMENT_TARGET=${CMAKE_OSX_DEPLOYMENT_TARGET} \
            -DCMAKE_OSX_SYSROOT=${CMAKE_OSX_SYSROOT} \
            -DCMAKE_CXX_FLAGS="-I$ABSL_SUBMODULE"

          cmake --build build --parallel
          test -f build/libtg_owt.a || { echo "tg_owt build failed"; exit 1; }

      - name: Cache tde2e (TDLib e2e)
        id: cache-tde2e
        uses: actions/cache@v4
        with:
          path: ${{ env.LibrariesPath }}/tde2e
          key: ${{ runner.os }}-tde2e

      - name: Build tde2e
        if: steps.cache-tde2e.outputs.cache-hit != 'true'
        run: |
          cd "$LibrariesPath"
          git init tde2e
          cd tde2e
          git remote add origin "$GIT/tdlib/td.git"
          git fetch --depth=1 origin 51743dfd01dff6179e2d8f7095729caa4e2222e9
          git reset --hard FETCH_HEAD

          cmake -B build -GNinja . \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_INSTALL_PREFIX="$PWD/build/prefix" \
            -DCMAKE_OSX_DEPLOYMENT_TARGET=${CMAKE_OSX_DEPLOYMENT_TARGET} \
            -DCMAKE_OSX_SYSROOT=${CMAKE_OSX_SYSROOT} \
            -DTD_E2E_ONLY=ON

          cmake --build build --parallel
          cmake --install build

      - name: Build Telegram Desktop
        env:
          tg_owt_DIR: ${{ env.LibrariesPath }}/tg_owt/build
          tde2e_DIR: ${{ env.LibrariesPath }}/tde2e/build/prefix
        run: |
          cd "$REPO_NAME"

          # Приоритет abseil из tg_owt + заголовки (обход jpeglib.h от Mono, иногда попадается)
          ABSL_SUBMODULE="$LibrariesPath/tg_owt/src/third_party/abseil-cpp"
          export CPATH="$ABSL_SUBMODULE:$CPATH"
          EXTRA_INC="/opt/homebrew/include"

          echo "FFmpeg versions (pkg-config):"
          pkg-config --modversion libavformat
          pkg-config --modversion libavcodec
          pkg-config --modversion libavutil

          cmake -B build -GNinja . \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_OSX_DEPLOYMENT_TARGET=${CMAKE_OSX_DEPLOYMENT_TARGET} \
            -DCMAKE_OSX_SYSROOT=${CMAKE_OSX_SYSROOT} \
            -DTDESKTOP_API_TEST=ON \
            -DCMAKE_PREFIX_PATH="${CMAKE_PREFIX_PATH}" \
            -DCMAKE_CXX_FLAGS="-I$ABSL_SUBMODULE -I$EXTRA_INC"

          cmake --build build --parallel

          cd build
          /opt/homebrew/opt/qt/bin/macdeployqt Telegram.app -verbose=3 -always-overwrite

      - name: Bundle non-Qt dylibs (FFmpeg, HEIF, AVIF, JXL, etc.)
        run: |
          APP="$REPO_NAME/build/Telegram.app"
          FW="$APP/Contents/Frameworks"
          BIN="$APP/Contents/MacOS/Telegram"

          mkdir -p "$FW"

          # Системные фреймворки/библиотеки и Qt — не трогаем
          is_ignored_ref() {
            case "$1" in
              @*|/System/*|/usr/lib/*|/System/Library/*|/Library/Frameworks/*|$APP/*|$FW/*|*/Qt*.framework/*)
                return 0 ;;
              *) return 1 ;;
            esac
          }

          copy_fix_dylib() {
            local libpath="$1"
            local base="$(basename "$libpath")"
            local dst="$FW/$base"

            # Копируем один раз
            if [ ! -f "$dst" ]; then
              echo "Bundling $libpath -> $dst"
              cp -a "$libpath" "$dst"
              chmod 755 "$dst"
              # Собственный id dylib → @rpath
              install_name_tool -id "@rpath/$base" "$dst"
            fi

            # Чиним ссылки зависимости у этой dylib рекурсивно
            while IFS= read -r dep; do
              dep="$(echo "$dep" | awk '{print $1}')" || true
              is_ignored_ref "$dep" && continue

              # Ищем в Homebrew (обычно /opt/homebrew/*)
              if [ -f "$dep" ]; then
                copy_fix_dylib "$dep"
                local depbase="$(basename "$dep")"
                install_name_tool -change "$dep" "@rpath/$depbase" "$dst"
              fi
            done < <(otool -L "$dst" | tail -n +2 | sed 's/(.*)//')
          }

          # Собираем список всех зависимостей бинарника, которые НЕ системные и НЕ Qt
          DEPS=$(otool -L "$BIN" | tail -n +2 | sed 's/(.*)//' | awk '{print $1}')
          for dep in $DEPS; do
            if ! is_ignored_ref "$dep" && [ -f "$dep" ]; then
              copy_fix_dylib "$dep"
              depbase="$(basename "$dep")"
              install_name_tool -change "$dep" "@rpath/$depbase" "$BIN"
            fi
          done

          # Добавляем rpath, чтобы бинарник видел Frameworks
          install_name_tool -add_rpath "@executable_path/../Frameworks" "$BIN" || true

          echo "Bundled non-Qt dylibs into $FW"

      - name: Codesign, DMG
        run: |
          APP="$REPO_NAME/build/Telegram.app"

          # ad-hoc подпись (достаточно для локального запуска/CI-артефакта)
          codesign -s - --force --deep -v "$APP"

          mkdir -p "$REPO_NAME/build/dmgsrc"
          mv "$APP" "$REPO_NAME/build/dmgsrc"
          hdiutil create -volname Telegram -srcfolder "$REPO_NAME/build/dmgsrc" -ov -format UDZO "$REPO_NAME/build/Telegram.dmg"

      - name: Upload artifact
        if: env.UPLOAD_ARTIFACT == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: Telegram_macOS14_release
          path: ${{ env.REPO_NAME }}/build/Telegram.dmg
