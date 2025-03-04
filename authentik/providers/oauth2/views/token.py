"""authentik OAuth2 Token views"""
from base64 import urlsafe_b64encode
from dataclasses import InitVar, dataclass
from hashlib import sha256
from re import error as RegexError
from re import fullmatch
from typing import Any, Optional

from django.http import HttpRequest, HttpResponse
from django.utils.timezone import datetime, now
from django.views import View
from jwt import PyJWK, PyJWTError, decode
from sentry_sdk.hub import Hub
from structlog.stdlib import get_logger

from authentik.core.models import (
    USER_ATTRIBUTE_EXPIRES,
    USER_ATTRIBUTE_GENERATED,
    Application,
    Token,
    TokenIntents,
    User,
)
from authentik.crypto.models import CertificateKeyPair
from authentik.events.models import Event, EventAction
from authentik.lib.utils.time import timedelta_from_string
from authentik.policies.engine import PolicyEngine
from authentik.providers.oauth2.constants import (
    CLIENT_ASSERTION,
    CLIENT_ASSERTION_TYPE,
    CLIENT_ASSERTION_TYPE_JWT,
    GRANT_TYPE_AUTHORIZATION_CODE,
    GRANT_TYPE_CLIENT_CREDENTIALS,
    GRANT_TYPE_PASSWORD,
    GRANT_TYPE_REFRESH_TOKEN,
)
from authentik.providers.oauth2.errors import TokenError, UserAuthError
from authentik.providers.oauth2.models import (
    AuthorizationCode,
    ClientTypes,
    JWTAlgorithms,
    OAuth2Provider,
    RefreshToken,
)
from authentik.providers.oauth2.utils import TokenResponse, cors_allow, extract_client_auth
from authentik.sources.oauth.models import OAuthSource

LOGGER = get_logger()


@dataclass
# pylint: disable=too-many-instance-attributes
class TokenParams:
    """Token params"""

    client_id: str
    client_secret: str
    redirect_uri: str
    grant_type: str
    state: str
    scope: list[str]

    provider: OAuth2Provider

    authorization_code: Optional[AuthorizationCode] = None
    refresh_token: Optional[RefreshToken] = None
    user: Optional[User] = None

    code_verifier: Optional[str] = None

    raw_code: InitVar[str] = ""
    raw_token: InitVar[str] = ""
    request: InitVar[Optional[HttpRequest]] = None

    @staticmethod
    def parse(
        request: HttpRequest,
        provider: OAuth2Provider,
        client_id: str,
        client_secret: str,
    ) -> "TokenParams":
        """Parse params for request"""
        return TokenParams(
            # Init vars
            raw_code=request.POST.get("code", ""),
            raw_token=request.POST.get("refresh_token", ""),
            request=request,
            # Regular params
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=request.POST.get("redirect_uri", "").lower(),
            grant_type=request.POST.get("grant_type", ""),
            state=request.POST.get("state", ""),
            scope=request.POST.get("scope", "").split(),
            # PKCE parameter.
            code_verifier=request.POST.get("code_verifier"),
        )

    def __check_policy_access(self, app: Application, request: HttpRequest, **kwargs):
        with Hub.current.start_span(
            op="authentik.providers.oauth2.token.policy",
        ):
            engine = PolicyEngine(app, self.user, request)
            engine.request.context["oauth_scopes"] = self.scope
            engine.request.context["oauth_grant_type"] = self.grant_type
            engine.request.context["oauth_code_verifier"] = self.code_verifier
            engine.request.context.update(kwargs)
            engine.build()
            result = engine.result
            if not result.passing:
                LOGGER.info("User not authenticated for application", user=self.user, app=app)
                raise TokenError("invalid_grant")

    def __post_init__(self, raw_code: str, raw_token: str, request: HttpRequest):
        if self.grant_type in [GRANT_TYPE_AUTHORIZATION_CODE, GRANT_TYPE_REFRESH_TOKEN]:
            if (
                self.provider.client_type == ClientTypes.CONFIDENTIAL
                and self.provider.client_secret != self.client_secret
            ):
                LOGGER.warning(
                    "Invalid client secret",
                    client_id=self.provider.client_id,
                )
                raise TokenError("invalid_client")

        if self.grant_type == GRANT_TYPE_AUTHORIZATION_CODE:
            with Hub.current.start_span(
                op="authentik.providers.oauth2.post.parse.code",
            ):
                self.__post_init_code(raw_code, request)
        elif self.grant_type == GRANT_TYPE_REFRESH_TOKEN:
            with Hub.current.start_span(
                op="authentik.providers.oauth2.post.parse.refresh",
            ):
                self.__post_init_refresh(raw_token, request)
        elif self.grant_type in [GRANT_TYPE_CLIENT_CREDENTIALS, GRANT_TYPE_PASSWORD]:
            with Hub.current.start_span(
                op="authentik.providers.oauth2.post.parse.client_credentials",
            ):
                self.__post_init_client_credentials(request)
        else:
            LOGGER.warning("Invalid grant type", grant_type=self.grant_type)
            raise TokenError("unsupported_grant_type")

    def __post_init_code(self, raw_code: str, request: HttpRequest):
        if not raw_code:
            LOGGER.warning("Missing authorization code")
            raise TokenError("invalid_grant")

        allowed_redirect_urls = self.provider.redirect_uris.split()
        # At this point, no provider should have a blank redirect_uri, in case they do
        # this will check an empty array and raise an error
        try:
            if not any(fullmatch(x, self.redirect_uri) for x in allowed_redirect_urls):
                LOGGER.warning(
                    "Invalid redirect uri",
                    redirect_uri=self.redirect_uri,
                    expected=allowed_redirect_urls,
                )
                Event.new(
                    EventAction.CONFIGURATION_ERROR,
                    message="Invalid redirect URI used by provider",
                    provider=self.provider,
                    redirect_uri=self.redirect_uri,
                    expected=allowed_redirect_urls,
                ).from_http(request)
                raise TokenError("invalid_client")
        except RegexError as exc:
            LOGGER.warning("Invalid regular expression configured", exc=exc)
            Event.new(
                EventAction.CONFIGURATION_ERROR,
                message="Invalid redirect_uri RegEx configured",
                provider=self.provider,
            ).from_http(request)
            raise TokenError("invalid_client")

        try:
            self.authorization_code = AuthorizationCode.objects.get(code=raw_code)
            if self.authorization_code.is_expired:
                LOGGER.warning(
                    "Code is expired",
                    token=raw_code,
                )
                raise TokenError("invalid_grant")
        except AuthorizationCode.DoesNotExist:
            LOGGER.warning("Code does not exist", code=raw_code)
            raise TokenError("invalid_grant")

        if self.authorization_code.provider != self.provider or self.authorization_code.is_expired:
            LOGGER.warning("Invalid code: invalid client or code has expired")
            raise TokenError("invalid_grant")

        # Validate PKCE parameters.
        if self.code_verifier:
            if self.authorization_code.code_challenge_method == "S256":
                new_code_challenge = (
                    urlsafe_b64encode(sha256(self.code_verifier.encode("ascii")).digest())
                    .decode("utf-8")
                    .replace("=", "")
                )
            else:
                new_code_challenge = self.code_verifier

            if new_code_challenge != self.authorization_code.code_challenge:
                LOGGER.warning("Code challenge not matching")
                raise TokenError("invalid_grant")

    def __post_init_refresh(self, raw_token: str, request: HttpRequest):
        if not raw_token:
            LOGGER.warning("Missing refresh token")
            raise TokenError("invalid_grant")

        try:
            self.refresh_token = RefreshToken.objects.get(
                refresh_token=raw_token, provider=self.provider
            )
            if self.refresh_token.is_expired:
                LOGGER.warning(
                    "Refresh token is expired",
                    token=raw_token,
                )
                raise TokenError("invalid_grant")
            # https://tools.ietf.org/html/rfc6749#section-6
            # Fallback to original token's scopes when none are given
            if not self.scope:
                self.scope = self.refresh_token.scope
        except RefreshToken.DoesNotExist:
            LOGGER.warning(
                "Refresh token does not exist",
                token=raw_token,
            )
            raise TokenError("invalid_grant")
        if self.refresh_token.revoked:
            LOGGER.warning("Refresh token is revoked", token=raw_token)
            Event.new(
                action=EventAction.SUSPICIOUS_REQUEST,
                message="Revoked refresh token was used",
                token=raw_token,
            ).from_http(request)
            raise TokenError("invalid_grant")

    def __post_init_client_credentials(self, request: HttpRequest):
        if request.POST.get(CLIENT_ASSERTION_TYPE, "") != "":
            return self.__post_init_client_credentials_jwt(request)
        # Authenticate user based on credentials
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = User.objects.filter(username=username).first()
        if not user:
            raise TokenError("invalid_grant")
        token: Token = Token.filter_not_expired(
            key=password, intent=TokenIntents.INTENT_APP_PASSWORD
        ).first()
        if not token or token.user.uid != user.uid:
            raise TokenError("invalid_grant")
        self.user = user
        # Authorize user access
        app = Application.objects.filter(provider=self.provider).first()
        if not app or not app.provider:
            raise TokenError("invalid_grant")
        self.__check_policy_access(app, request)

        Event.new(
            action=EventAction.LOGIN,
            PLAN_CONTEXT_METHOD="token",
            PLAN_CONTEXT_METHOD_ARGS={
                "identifier": token.identifier,
            },
            PLAN_CONTEXT_APPLICATION=app,
        ).from_http(request, user=user)
        return None

    # pylint: disable=too-many-locals
    def __post_init_client_credentials_jwt(self, request: HttpRequest):
        assertion_type = request.POST.get(CLIENT_ASSERTION_TYPE, "")
        if assertion_type != CLIENT_ASSERTION_TYPE_JWT:
            LOGGER.warning("Invalid assertion type", assertion_type=assertion_type)
            raise TokenError("invalid_grant")

        client_secret = request.POST.get("client_secret", None)
        assertion = request.POST.get(CLIENT_ASSERTION, client_secret)
        if not assertion:
            LOGGER.warning("Missing client assertion")
            raise TokenError("invalid_grant")

        token = None

        # TODO: Remove in 2022.7, deprecated field `verification_keys``
        for cert in self.provider.verification_keys.all():
            LOGGER.debug("verifying jwt with key", key=cert.name)
            cert: CertificateKeyPair
            public_key = cert.certificate.public_key()
            if cert.private_key:
                public_key = cert.private_key.public_key()
            try:
                token = decode(
                    assertion,
                    public_key,
                    algorithms=[JWTAlgorithms.RS256, JWTAlgorithms.ES256],
                    options={
                        "verify_aud": False,
                    },
                )
            except (PyJWTError, ValueError, TypeError) as exc:
                LOGGER.warning("failed to validate jwt", exc=exc)
        # TODO: End remove block

        source: Optional[OAuthSource] = None
        parsed_key: Optional[PyJWK] = None
        for source in self.provider.jwks_sources.all():
            LOGGER.debug("verifying jwt with source", source=source.name)
            keys = source.oidc_jwks.get("keys", [])
            for key in keys:
                LOGGER.debug("verifying jwt with key", source=source.name, key=key.get("kid"))
                try:
                    parsed_key = PyJWK.from_dict(key)
                    token = decode(
                        assertion,
                        parsed_key.key,
                        algorithms=[key.get("alg")],
                        options={
                            "verify_aud": False,
                        },
                    )
                # AttributeError is raised when the configured JWK is a private key
                # and not a public key
                except (PyJWTError, ValueError, TypeError, AttributeError) as exc:
                    LOGGER.warning("failed to validate jwt", exc=exc)

        if not token:
            LOGGER.warning("No token could be verified")
            raise TokenError("invalid_grant")

        if "exp" in token:
            exp = datetime.fromtimestamp(token["exp"])
            # Non-timezone aware check since we assume `exp` is in UTC
            if datetime.now() >= exp:
                LOGGER.info("JWT token expired")
                raise TokenError("invalid_grant")

        app = Application.objects.filter(provider=self.provider).first()
        if not app or not app.provider:
            LOGGER.info("client_credentials grant for provider without application")
            raise TokenError("invalid_grant")

        self.__check_policy_access(app, request, oauth_jwt=token)
        self.__create_user_from_jwt(token, app)

        method_args = {
            "jwt": token,
        }
        if source:
            method_args["source"] = source
        if parsed_key:
            method_args["jwk_id"] = parsed_key.key_id
        Event.new(
            action=EventAction.LOGIN,
            PLAN_CONTEXT_METHOD="jwt",
            PLAN_CONTEXT_METHOD_ARGS=method_args,
            PLAN_CONTEXT_APPLICATION=app,
        ).from_http(request, user=self.user)

    def __create_user_from_jwt(self, token: dict[str, Any], app: Application):
        """Create user from JWT"""
        exp = token.get("exp")
        self.user, created = User.objects.update_or_create(
            username=f"{self.provider.name}-{token.get('sub')}",
            defaults={
                "attributes": {
                    USER_ATTRIBUTE_GENERATED: True,
                },
                "last_login": now(),
                "name": f"Autogenerated user from application {app.name} (client credentials JWT)",
            },
        )
        if created and exp:
            self.user.attributes[USER_ATTRIBUTE_EXPIRES] = exp
            self.user.save()


class TokenView(View):
    """Generate tokens for clients"""

    provider: Optional[OAuth2Provider] = None
    params: Optional[TokenParams] = None

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response = super().dispatch(request, *args, **kwargs)
        allowed_origins = []
        if self.provider:
            allowed_origins = self.provider.redirect_uris.split("\n")
        cors_allow(self.request, response, *allowed_origins)
        return response

    def options(self, request: HttpRequest) -> HttpResponse:
        return TokenResponse({})

    def post(self, request: HttpRequest) -> HttpResponse:
        """Generate tokens for clients"""
        try:
            with Hub.current.start_span(
                op="authentik.providers.oauth2.post.parse",
            ):
                client_id, client_secret = extract_client_auth(request)
                try:
                    self.provider = OAuth2Provider.objects.get(client_id=client_id)
                except OAuth2Provider.DoesNotExist:
                    LOGGER.warning("OAuth2Provider does not exist", client_id=client_id)
                    raise TokenError("invalid_client")

                if not self.provider:
                    raise ValueError
                self.params = TokenParams.parse(request, self.provider, client_id, client_secret)

            with Hub.current.start_span(
                op="authentik.providers.oauth2.post.response",
            ):
                if self.params.grant_type == GRANT_TYPE_AUTHORIZATION_CODE:
                    LOGGER.debug("Converting authorization code to refresh token")
                    return TokenResponse(self.create_code_response())
                if self.params.grant_type == GRANT_TYPE_REFRESH_TOKEN:
                    LOGGER.debug("Refreshing refresh token")
                    return TokenResponse(self.create_refresh_response())
                if self.params.grant_type == GRANT_TYPE_CLIENT_CREDENTIALS:
                    LOGGER.debug("Client credentials grant")
                    return TokenResponse(self.create_client_credentials_response())
                raise ValueError(f"Invalid grant_type: {self.params.grant_type}")
        except TokenError as error:
            return TokenResponse(error.create_dict(), status=400)
        except UserAuthError as error:
            return TokenResponse(error.create_dict(), status=403)

    def create_code_response(self) -> dict[str, Any]:
        """See https://tools.ietf.org/html/rfc6749#section-4.1"""

        refresh_token = self.params.authorization_code.provider.create_refresh_token(
            user=self.params.authorization_code.user,
            scope=self.params.authorization_code.scope,
            request=self.request,
        )

        if self.params.authorization_code.is_open_id:
            id_token = refresh_token.create_id_token(
                user=self.params.authorization_code.user,
                request=self.request,
            )
            id_token.nonce = self.params.authorization_code.nonce
            id_token.at_hash = refresh_token.at_hash
            refresh_token.id_token = id_token

        # Store the token.
        refresh_token.save()

        # We don't need to store the code anymore.
        self.params.authorization_code.delete()

        return {
            "access_token": refresh_token.access_token,
            "refresh_token": refresh_token.refresh_token,
            "token_type": "bearer",
            "expires_in": int(
                timedelta_from_string(self.params.provider.token_validity).total_seconds()
            ),
            "id_token": refresh_token.provider.encode(refresh_token.id_token.to_dict()),
        }

    def create_refresh_response(self) -> dict[str, Any]:
        """See https://tools.ietf.org/html/rfc6749#section-6"""

        unauthorized_scopes = set(self.params.scope) - set(self.params.refresh_token.scope)
        if unauthorized_scopes:
            raise TokenError("invalid_scope")

        provider: OAuth2Provider = self.params.refresh_token.provider

        refresh_token: RefreshToken = provider.create_refresh_token(
            user=self.params.refresh_token.user,
            scope=self.params.scope,
            request=self.request,
        )

        # If the Token has an id_token it's an Authentication request.
        if self.params.refresh_token.id_token:
            refresh_token.id_token = refresh_token.create_id_token(
                user=self.params.refresh_token.user,
                request=self.request,
            )
            refresh_token.id_token.at_hash = refresh_token.at_hash

            # Store the refresh_token.
            refresh_token.save()

        # Mark old token as revoked
        self.params.refresh_token.revoked = True
        self.params.refresh_token.save()

        return {
            "access_token": refresh_token.access_token,
            "refresh_token": refresh_token.refresh_token,
            "token_type": "bearer",
            "expires_in": int(
                timedelta_from_string(refresh_token.provider.token_validity).total_seconds()
            ),
            "id_token": self.params.provider.encode(refresh_token.id_token.to_dict()),
        }

    def create_client_credentials_response(self) -> dict[str, Any]:
        """See https://datatracker.ietf.org/doc/html/rfc6749#section-4.4"""
        provider: OAuth2Provider = self.params.provider

        refresh_token: RefreshToken = provider.create_refresh_token(
            user=self.params.user,
            scope=self.params.scope,
            request=self.request,
        )
        refresh_token.id_token = refresh_token.create_id_token(
            user=self.params.user,
            request=self.request,
        )
        refresh_token.id_token.at_hash = refresh_token.at_hash

        # Store the refresh_token.
        refresh_token.save()

        return {
            "access_token": refresh_token.access_token,
            "token_type": "bearer",
            "expires_in": int(
                timedelta_from_string(refresh_token.provider.token_validity).total_seconds()
            ),
            "id_token": self.params.provider.encode(refresh_token.id_token.to_dict()),
        }
