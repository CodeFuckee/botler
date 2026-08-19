"""SSO 登录 API（issue #27）：状态探测 / 登录跳转 / 回调 / 退出。

流程：前端经 /api/auth/status 判断是否需要登录 → /api/auth/login 302 到群晖
SSO Server → 用户认证后回调 /api/auth/callback → 会话 cookie 落盘 → 302 回首页。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth import CSRF_COOKIE, SESSION_COOKIE, STATE_COOKIE, STATE_TTL_SECONDS

router = APIRouter(prefix="/auth", tags=["auth"])

# 会话有效期上限（天），防误配过长
MAX_SESSION_DAYS = 365


def _sso(request: Request):
    return request.app.state.ctx.sso


@router.get("/status")
def auth_status(request: Request):
    """SSO 启用状态 + 当前用户（未登录 user 为 null）。"""
    sso = _sso(request)
    return {"enabled": sso.enabled(), "user": sso.current_user(request)}


@router.get("/login")
def auth_login(request: Request):
    """跳转群晖 SSO 登录页（302）。"""
    sso = _sso(request)
    if not sso.enabled():
        raise HTTPException(400, "SSO 未启用，无需登录")
    url, state = sso.build_login(request)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE, state,
        max_age=STATE_TTL_SECONDS, httponly=True, samesite="lax",
    )
    return resp


@router.get("/callback")
def auth_callback(request: Request, code: str | None = None,
                  state: str | None = None, error: str | None = None):
    """群晖认证后回调：换 token → 建会话 → 回首页；失败回登录页并提示。"""
    if error:
        return RedirectResponse(f"/login?error={error}", status_code=302)
    sso = _sso(request)
    session_cookie = sso.complete_login(request, code, state)
    if session_cookie is None:
        return RedirectResponse("/login?error=login_failed", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    days = min(sso.config.get().sso_session_days or 7, MAX_SESSION_DAYS)
    resp.set_cookie(
        SESSION_COOKIE, session_cookie,
        max_age=days * 86400, httponly=True, samesite="lax",
    )
    # 下发 CSRF token（issue #263）：双提交 cookie 模式，值由会话密钥
    # 派生并绑定本会话；非 HttpOnly——前端 api.js 需读取回填请求头
    resp.set_cookie(
        CSRF_COOKIE, sso.csrf_token_for(session_cookie),
        max_age=days * 86400, httponly=False, samesite="lax",
    )
    # 清除一次性 state cookie
    resp.delete_cookie(STATE_COOKIE)
    return resp


@router.post("/logout")
def auth_logout(request: Request):
    """退出登录：清除会话 cookie。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(CSRF_COOKIE)
    return resp


@router.get("/me")
def auth_me(request: Request):
    """当前登录用户信息（未登录 401，供前端兜底判断）。"""
    sso = _sso(request)
    user = sso.current_user(request)
    if user is None:
        raise HTTPException(401, "未登录")
    # 老会话补发 CSRF cookie（issue #263）：登录早于 CSRF 防护上线的
    # 会话没有 botler_csrf，前端启动探测本端点时一并下发，避免升级后
    # 写操作因缺 cookie 一直 403（写请求本身缺失仍 403，不给攻击窗口）
    if request.cookies.get(CSRF_COOKIE) is None:
        session = request.cookies.get(SESSION_COOKIE)
        resp = JSONResponse(user)
        resp.set_cookie(
            CSRF_COOKIE, sso.csrf_token_for(session),
            max_age=min(sso.config.get().sso_session_days or 7, MAX_SESSION_DAYS) * 86400,
            httponly=False, samesite="lax",
        )
        return resp
    return user
