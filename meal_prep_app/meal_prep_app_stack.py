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
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct

ADMINS_GROUP_NAME = "Admins"


class MealPrepAppStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
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
                "ADMINS_GROUP_NAME": ADMINS_GROUP_NAME,
                # Empty by default (real Lambda uses the real AWS endpoint);
                # local-env.json overrides this for `sam local` / testing.
                "DYNAMODB_ENDPOINT_OVERRIDE": "",
            },
        )
        table.grant_read_write_data(api_fn)

        # ---------- S3 + CloudFront (static site) ----------
        # Built before the API so its domain is available for the API's CORS config.
        site_bucket = s3.Bucket(
            self, "SiteBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        distribution = cloudfront.Distribution(
            self, "SiteDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )

        # ---------- API Gateway (HTTP API) ----------
        http_api = apigwv2.HttpApi(
            self, "MealPrepHttpApi",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[f"https://{distribution.distribution_domain_name}"],
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

        # Orders: any signed-in customer, scoped to their own orders.
        http_api.add_routes(
            path="/orders", methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
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
            path="/addons/{addOnId}", methods=[apigwv2.HttpMethod.DELETE],
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
        )

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionDomainName", value=distribution.distribution_domain_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
