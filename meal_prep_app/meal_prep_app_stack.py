from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3_deployment as s3_deployment,
    aws_certificatemanager as acm,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

ADMINS_GROUP_NAME = "Admins"


class MealPrepAppStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        domain_name: str | None = None,
        certificate_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------- DynamoDB ----------
        table_name = "MealPrepTable"
        table = dynamodb.Table(
            self, "MealPrepTable",
            table_name=table_name,
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # fine for dev; revisit for prod
        )

        # ---------- Cognito (customer accounts) ----------
        user_pool = cognito.UserPool(
            self, "UserPool",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                fullname=cognito.StandardAttribute(required=False, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=False,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,  # fine for dev; revisit for prod
        )

        user_pool_client = user_pool.add_client(
            "WebClient",
            generate_secret=False,  # public browser client
            auth_flows=cognito.AuthFlow(user_password=True),
            prevent_user_existence_errors=True,
        )

        # Membership is granted manually (aws cognito-idp admin-add-user-to-group) -
        # self-signup never grants admin.
        cognito.CfnUserPoolGroup(
            self, "AdminsGroup",
            user_pool_id=user_pool.user_pool_id,
            group_name=ADMINS_GROUP_NAME,
            description="Users who can manage the meal menu",
        )

        # Twilio Account SID / Auth Token / From number - a real secret (the
        # Auth Token can send SMS on the account and rack up charges), so it
        # lives in Secrets Manager rather than DynamoDB settings or an env
        # var, and is never exposed through any API response. Starts empty;
        # populate it after deploying with:
        #   aws secretsmanager put-secret-value --profile gtx-meal-prep \
        #     --secret-id <arn from stack output> \
        #     --secret-string '{"accountSid":"AC...","authToken":"...","fromNumber":"+1..."}'
        twilio_secret = secretsmanager.Secret(
            self, "TwilioCredentials",
            description="Twilio Account SID, Auth Token, and From number for sending customer SMS",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------- Lambda ----------
        api_fn = lambda_.Function(
            self, "ApiFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset("backend/lambda_src"),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "TABLE_NAME": table_name,
                "USER_POOL_ID": user_pool.user_pool_id,
                "ADMINS_GROUP_NAME": ADMINS_GROUP_NAME,
                "TWILIO_SECRET_ARN": twilio_secret.secret_arn,
                # No FROM_EMAIL setting - the Lambda resolves the sender
                # identity at send time from whoever is actually in the
                # Admins group (see _from_email() in index.py), rather than
                # a hardcoded address that has to be kept in sync by hand.
                # Whatever that address is must still be verified in SES
                # before any email actually sends - see README.
                # Empty by default (real Lambda uses the real AWS endpoint);
                # local-env.json overrides this for `sam local` / testing.
                "DYNAMODB_ENDPOINT_OVERRIDE": "",
            },
        )
        table.grant_read_write_data(api_fn)
        twilio_secret.grant_read(api_fn)

        # Weekly reminders: SES for email, Twilio (not SNS - this AWS org's
        # SCP blocks sns:Publish/sms-voice:* entirely, see README) for text.
        # SES doesn't support per-resource scoping for sending, so this is
        # an account-wide grant, standard practice for this permission.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"],
        ))

        # Admin customer management: list every account, resolve a customer's
        # sub from their login email, remove an account on request, and look
        # up admin emails to notify on new orders.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cognito-idp:ListUsers", "cognito-idp:ListUsersInGroup",
                     "cognito-idp:AdminGetUser", "cognito-idp:AdminDeleteUser"],
            resources=[user_pool.user_pool_arn],
        ))

        # ---------- S3 + CloudFront (static site) ----------
        # Built before the API so its domain is available for the API's CORS config.
        site_bucket = s3.Bucket(
            self, "SiteBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        # Appends .html to extensionless paths (e.g. /admin -> /admin.html) so
        # pages can be linked with clean URLs without renaming any files, and
        # (once a custom domain is attached) redirects www to the apex.
        clean_urls_function = cloudfront.Function(
            self, "CleanUrlsFunction",
            code=cloudfront.FunctionCode.from_file(
                file_path="meal_prep_app/cloudfront_functions/clean_urls.js"
            ),
            comment="Appends .html to extensionless paths; redirects www to the apex",
        )

        # The certificate must already be issued (DNS-validated) before this
        # deploys - imported by ARN because it has to live in us-east-1 for
        # CloudFront, and this account's org-level SCP blocks CloudFormation
        # entirely in that Region, so it's requested/validated outside CDK.
        certificate = (
            acm.Certificate.from_certificate_arn(self, "ImportedCertificate", certificate_arn)
            if certificate_arn else None
        )

        distribution_kwargs = {}
        if domain_name and certificate:
            distribution_kwargs["domain_names"] = [domain_name, f"www.{domain_name}"]
            distribution_kwargs["certificate"] = certificate

        distribution = cloudfront.Distribution(
            self, "SiteDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=clean_urls_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            **distribution_kwargs,
        )

        # Reminder messages link back to the site; prefer the custom domain
        # once it's attached, since that's the one customers actually see.
        site_url = f"https://{domain_name}" if domain_name else f"https://{distribution.distribution_domain_name}"
        api_fn.add_environment("SITE_URL", site_url)

        # ---------- API Gateway (HTTP API) ----------
        allowed_origins = [f"https://{distribution.distribution_domain_name}"]
        if domain_name:
            allowed_origins.append(f"https://{domain_name}")
            allowed_origins.append(f"https://www.{domain_name}")

        http_api = apigwv2.HttpApi(
            self, "MealPrepHttpApi",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=allowed_origins,
                allow_methods=[apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST,
                               apigwv2.CorsHttpMethod.PUT, apigwv2.CorsHttpMethod.DELETE],
                allow_headers=["authorization", "content-type"],
            ),
        )

        integration = apigwv2_integrations.HttpLambdaIntegration("ApiIntegration", api_fn)

        jwt_authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        # Menu: browsing is public; managing it requires an Admins-group member.
        http_api.add_routes(
            path="/meals", methods=[apigwv2.HttpMethod.GET], integration=integration,
        )
        http_api.add_routes(
            path="/meals", methods=[apigwv2.HttpMethod.POST], integration=integration,
            authorizer=jwt_authorizer,
        )
        http_api.add_routes(
            path="/meals/{mealId}", methods=[apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Global settings (currently just the next ready date): browsing is
        # public so the menu page can show it; only the admin can change it.
        http_api.add_routes(
            path="/settings", methods=[apigwv2.HttpMethod.GET], integration=integration,
        )
        http_api.add_routes(
            path="/settings", methods=[apigwv2.HttpMethod.PUT], integration=integration,
            authorizer=jwt_authorizer,
        )

        # Orders: any signed-in customer, scoped to their own orders. PUT is
        # self-service cancel only (used by the "Edit" flow).
        http_api.add_routes(
            path="/orders", methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=integration, authorizer=jwt_authorizer,
        )
        http_api.add_routes(
            path="/orders/{orderId}", methods=[apigwv2.HttpMethod.PUT],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Admin order queue: list orders by status (defaults to "placed") across
        # every customer, and update an individual order's status.
        http_api.add_routes(
            path="/admin/orders", methods=[apigwv2.HttpMethod.GET],
            integration=integration, authorizer=jwt_authorizer,
        )
        http_api.add_routes(
            path="/admin/orders/{sub}/{orderId}", methods=[apigwv2.HttpMethod.PUT],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Profile: saved contact details, scoped to the caller.
        http_api.add_routes(
            path="/profile", methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Add-ons: a single global list (e.g. "Large", "Extra Protein") offered
        # on every meal. Browsing is public; managing the list is admin-only.
        http_api.add_routes(
            path="/addons", methods=[apigwv2.HttpMethod.GET], integration=integration,
        )
        http_api.add_routes(
            path="/addons", methods=[apigwv2.HttpMethod.POST], integration=integration,
            authorizer=jwt_authorizer,
        )
        http_api.add_routes(
            path="/addons/{addOnId}", methods=[apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Weekly reminders: admin-triggered broadcast (checked in the Lambda
        # via the Admins group, same as the other admin-only routes).
        http_api.add_routes(
            path="/reminders/send", methods=[apigwv2.HttpMethod.POST],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Customer accounts: admin-only (checked in the Lambda).
        http_api.add_routes(
            path="/customers", methods=[apigwv2.HttpMethod.GET],
            integration=integration, authorizer=jwt_authorizer,
        )
        http_api.add_routes(
            path="/customers/{username}", methods=[apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=integration, authorizer=jwt_authorizer,
        )

        # Resolved resource values (API URL, Cognito IDs) aren't known until this
        # deploy runs, so they're written into config.json alongside the static
        # assets rather than hardcoded into the frontend source.
        frontend_config = s3_deployment.Source.json_data("config.json", {
            "apiUrl": http_api.api_endpoint,
            "region": self.region,
            "userPoolId": user_pool.user_pool_id,
            "userPoolClientId": user_pool_client.user_pool_client_id,
        })

        s3_deployment.BucketDeployment(
            self, "SiteDeployment",
            sources=[s3_deployment.Source.asset("frontend"), frontend_config],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            # No cache-busting filename hashes on these assets, so without
            # this, a browser that heuristically cached an old JS/HTML file
            # right before a deploy can keep serving it well after - e.g. an
            # index.html that imports a function from pricing.js newer than
            # the browser's still-cached copy of pricing.js, which throws at
            # module-load time and silently kills the entire page script.
            # Forcing revalidation on every load (cheap: a 304 if unchanged)
            # closes that gap for every future deploy, not just this one.
            cache_control=[
                s3_deployment.CacheControl.no_cache(),
                s3_deployment.CacheControl.must_revalidate(),
            ],
        )

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionDomainName", value=distribution.distribution_domain_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "TwilioSecretArn", value=twilio_secret.secret_arn)
        if domain_name:
            CfnOutput(self, "SiteDomain", value=f"https://{domain_name}")
