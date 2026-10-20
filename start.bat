@echo off
title 蛋白计算平台 - 启动器
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  本文件必须存成 GBK(cp936) + CRLF。Windows 控制台默认代码页是 GBK，
rem  中文注释和 echo 文本都按 GBK 解读。绝不要在文件里加 chcp 65001，
rem  也不要把它存成 UTF-8 —— 那样 cmd 会把中文部分当成命令来跑，
rem  报 'xxx' 不是内部或外部命令。路径写死在这里，不依赖 PATH 上的 python。
rem ---------------------------------------------------------------------

set "PYTHON=C:\ProgramData\anaconda3\python.exe"
set "URL=http://127.0.0.1:8473"

rem ---- 可选：载入 .env（没有这个文件就用代码里的默认值）---------------
rem 代码只从进程环境读变量，自己不加载 .env，所以在这里载入，
rem 后面拉起的三个服务窗口都会继承。# 开头的行会被跳过。
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%B"=="" set "%%A=%%B"
    )
)

rem 后台工种总开关（调度器 + 打包器 + 清理器）。必须开：不打开的话
rem 任务提交后永远停在「排队中」。默认关闭只是为了测试，生产必开。
if not defined PROTFORGE_WEB_WORKERS set "PROTFORGE_WEB_WORKERS=1"

if not exist "%PYTHON%" (
    echo [错误] 找不到 Python：%PYTHON%
    echo.
    echo     请装一个 Python，或者改本文件里的 PYTHON 变量指向你实际用的解释器。
    echo     本机 PATH 上的 python 未必是它。
    echo.
    pause
    exit /b 1
)

rem ---- 探活：8473 已经在跑就别重复起 -----------------------------------
rem 打 /api/health 而不是只看端口：返回 200 才说明「这个端口后面确实是
rem 本平台的主服务」，能把「面板已经在跑」和「别的程序占了 8473」分开。
powershell -NoProfile -Command "try { if ((Invoke-WebRequest -UseBasicParsing '%URL%/api/health' -TimeoutSec 2).StatusCode -eq 200) { exit 0 } } catch { } ; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo 主服务已在运行，直接打开浏览器完成。
    start "" "%URL%"
    echo.
    echo   如果浏览器里是 404，先看那个服务窗口里的报错。
    echo.
    pause
    exit /b 0
)

echo ============================================================
echo   蛋白计算平台  启动器
echo ============================================================
echo.
echo   用户面板    %URL%               0.0.0.0:8473   反代只转发这个
echo   管理员面板  http://127.0.0.1:8474/             只在本机，别转发
echo   内部 API    http://127.0.0.1:8475/             只在本机，别转发
echo.
echo   数据盘      <数据盘根>
echo   调度器      由内部 API 进程(8475)带起来，不需要单独起
echo.
echo   浏览器会自动打开用户面板。那三个窗口才是服务，本窗口关掉不影响。
echo.
echo ============================================================
echo.

if not defined PROTFORGE_ADMIN_USER (
    echo [提醒] .env 里没设 PROTFORGE_ADMIN_USER。
    echo        库里如果还没有管理员，谁都登不进去，管理面板也进不去。
    echo        在 .env 里加上 PROTFORGE_ADMIN_USER 和 PROTFORGE_ADMIN_PASSWORD
    echo        之后重启，服务会自动创建这个管理员。
    echo.
)

rem uvicorn 要一会儿才绑上端口，所以延时一点再开浏览器，
rem 否则会先看到「无法访问此页面」。ping 当延时用；timeout 需要后台才有。
start "" /min cmd /c "ping -n 6 127.0.0.1 >nul & start %URL%/"

rem PYTHON 路径里没有空格，所以下面的命令一律不加引号：
rem start "标题" cmd /k "命令" 再套一层引号很容易被 cmd 拆错。
rem cmd /k 让窗口在崩溃后仍然留着，能看到 Python 的报错。
start "protforge-web 用户面板 8473" cmd /k %PYTHON% server\app.py public
start "protforge-web 内部 API 8475" cmd /k %PYTHON% server\app.py internal
start "protforge-web 管理员面板 8474" cmd /k %PYTHON% admin\app_admin.py

echo.
echo 三个服务窗口已经拉起来了。等几秒浏览器会自动打开用户面板。
echo.
echo   停服务：关掉那三个窗口，或在任务管理器里结束对应的 python 进程。
echo   看日志：两个主服务的输出都在各自那个窗口里（不走文件）。
echo.
echo 按任意键关闭本启动器窗口（不影响服务）。
echo.
pause
