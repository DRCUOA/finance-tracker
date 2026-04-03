from pydantic import BaseModel, EmailStr


class RegisterForm(BaseModel):
    email: str
    password: str
    display_name: str


class LoginForm(BaseModel):
    email: str
    password: str
