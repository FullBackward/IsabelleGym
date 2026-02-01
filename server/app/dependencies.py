from fastapi import Request

def get_session_manager(request: Request):
    return request.app.state.session_manager