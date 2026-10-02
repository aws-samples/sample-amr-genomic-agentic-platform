/**
 * WebSocket $connect JWT authorizer Lambda.
 * Validates a Cognito JWT from the query string (?token=...) and returns an IAM
 * policy allowing or denying execute-api:Invoke.
 *
 * Token verification uses aws-jwt-verify, the AWS-maintained library for Amazon
 * Cognito JWTs. It handles JWKS retrieval and caching, RS256 signature
 * verification, and issuer, audience, and expiry checks, so this Lambda does not
 * implement any cryptographic primitives itself.
 */
import { APIGatewayRequestAuthorizerEvent, APIGatewayAuthorizerResult } from 'aws-lambda';
import { CognitoJwtVerifier } from 'aws-jwt-verify';

const USER_POOL_ID = process.env.USER_POOL_ID!;
const CLIENT_ID = process.env.CLIENT_ID!;

// The verifier caches the pool's JWKS across invocations for warm reuse.
// tokenUse is null so both ID tokens (aud = client_id) and access tokens
// (client_id claim) from this pool and client are accepted.
const verifier = CognitoJwtVerifier.create({
  userPoolId: USER_POOL_ID,
  clientId: CLIENT_ID,
  tokenUse: null,
});

function buildPolicy(
  principalId: string,
  effect: 'Allow' | 'Deny',
  resource: string,
  context?: Record<string, string>,
): APIGatewayAuthorizerResult {
  return {
    principalId,
    policyDocument: {
      Version: '2012-10-17',
      Statement: [
        {
          Action: 'execute-api:Invoke',
          Effect: effect,
          Resource: resource,
        },
      ],
    },
    context: context ?? {},
  };
}

export const handler = async (
  event: APIGatewayRequestAuthorizerEvent,
): Promise<APIGatewayAuthorizerResult> => {
  console.log(JSON.stringify({ level: 'INFO', message: 'ws-authorizer invoked', methodArn: event.methodArn }));

  const token =
    event.queryStringParameters?.token ??
    event.queryStringParameters?.Token;

  if (!token) {
    console.log(JSON.stringify({ level: 'WARN', message: 'No token in query string' }));
    return buildPolicy('unauthenticated', 'Deny', event.methodArn);
  }

  try {
    const payload = await verifier.verify(token);
    const sub = (payload.sub as string) ?? (payload['cognito:username'] as string) ?? 'unknown';
    const email = (payload.email as string) ?? sub;

    console.log(JSON.stringify({ level: 'INFO', message: 'Token verified', sub, email }));

    return buildPolicy(sub, 'Allow', event.methodArn, {
      sub,
      email,
      userId: sub,
    });
  } catch (err) {
    console.log(JSON.stringify({ level: 'WARN', message: 'Token verification failed', error: String(err) }));
    return buildPolicy('unauthenticated', 'Deny', event.methodArn);
  }
};
