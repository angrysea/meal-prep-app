import aws_cdk as core
import aws_cdk.assertions as assertions

from meal_prep_app.meal_prep_app_stack import MealPrepAppStack


def test_dynamodb_table_created():
    app = core.App()
    stack = MealPrepAppStack(app, "meal-prep-app")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties("AWS::DynamoDB::Table", {
        "BillingMode": "PAY_PER_REQUEST",
    })


def test_lambda_and_http_api_created():
    app = core.App()
    stack = MealPrepAppStack(app, "meal-prep-app")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "index.handler",
        "Runtime": "python3.12",
    })
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)


def test_site_bucket_and_cloudfront_created():
    app = core.App()
    stack = MealPrepAppStack(app, "meal-prep-app")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::S3::Bucket", 1)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)


def test_cognito_user_pool_and_admins_group_created():
    app = core.App()
    stack = MealPrepAppStack(app, "meal-prep-app")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::Cognito::UserPool", 1)
    template.has_resource_properties("AWS::Cognito::UserPoolClient", {
        "GenerateSecret": False,
        "ExplicitAuthFlows": assertions.Match.array_with(["ALLOW_USER_PASSWORD_AUTH"]),
    })
    template.has_resource_properties("AWS::Cognito::UserPoolGroup", {
        "GroupName": "Admins",
    })


def test_api_routes_have_expected_authorization():
    app = core.App()
    stack = MealPrepAppStack(app, "meal-prep-app")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::ApiGatewayV2::Authorizer", 1)

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    routes_by_key = {r["Properties"]["RouteKey"]: r["Properties"] for r in routes.values()}

    assert routes_by_key["GET /meals"]["AuthorizationType"] == "NONE"
    assert routes_by_key["POST /meals"]["AuthorizationType"] == "JWT"
    assert routes_by_key["GET /orders"]["AuthorizationType"] == "JWT"
    assert routes_by_key["POST /orders"]["AuthorizationType"] == "JWT"
