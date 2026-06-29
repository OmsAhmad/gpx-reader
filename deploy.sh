#!/bin/bash
set -euo pipefail

# Configuration
# DEPLOY_PROFILE: AWS profile to use. Defaults to "hipallo" for local runs.
#   Set DEPLOY_PROFILE="" (e.g. in CI) to use ambient credentials.
# PUBLISH_ONLY=1: skip infra provisioning; just upload the site and invalidate
#   CloudFront. Used by the GitHub Action on content changes.
PROFILE="${DEPLOY_PROFILE-hipallo}"
DOMAIN="gpx.hipallo.com"
BUCKET="gpx.hipallo.com"
REGION="us-east-1"
HTML_SRC="GPX Viewer.html"
PUBLISH_ONLY="${PUBLISH_ONLY:-0}"

if [ -n "$PROFILE" ]; then
  AWS=(aws --profile "$PROFILE")
else
  AWS=(aws)
fi

upload_site() {
  echo "Uploading site..."
  "${AWS[@]}" s3 cp "$HTML_SRC" "s3://$BUCKET/index.html" \
    --content-type "text/html" \
    --cache-control "max-age=300"
}

invalidate_cf() {
  local dist_id
  dist_id=$("${AWS[@]}" cloudfront list-distributions \
    --query "DistributionList.Items[?Aliases.Items[0]=='$DOMAIN'].Id" --output text)
  if [ -n "$dist_id" ] && [ "$dist_id" != "None" ]; then
    echo "Invalidating CloudFront ($dist_id)..."
    "${AWS[@]}" cloudfront create-invalidation --distribution-id "$dist_id" \
      --paths "/index.html" "/" >/dev/null
    echo "Invalidation requested."
  else
    echo "WARNING: no CloudFront distribution found for $DOMAIN"
  fi
}

# Fast path used by CI: the bucket, cert, CloudFront, and DNS already exist;
# just push the new content and bust the cache.
if [ "$PUBLISH_ONLY" = "1" ]; then
  echo "=== Publishing $DOMAIN (content only) ==="
  upload_site
  invalidate_cf
  echo "Done."
  exit 0
fi

echo "=== Deploying $DOMAIN (full provision) ==="

# 1. Create S3 bucket (ignore error if exists)
echo "Creating S3 bucket..."
"${AWS[@]}" s3 mb "s3://$BUCKET" --region "$REGION" 2>/dev/null || true

# 2. Configure bucket for static website hosting
echo "Configuring static website hosting..."
"${AWS[@]}" s3 website "s3://$BUCKET" --index-document index.html

# 3. Disable block public access (must come before bucket policy)
echo "Disabling block public access..."
"${AWS[@]}" s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"

# Set bucket policy for public read
echo "Setting bucket policy..."
"${AWS[@]}" s3api put-bucket-policy --bucket "$BUCKET" --policy '{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadGetObject",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::'"$BUCKET"'/*"
  }]
}'

# 4. Upload the single-file app as index.html
upload_site

# 5. Request ACM certificate (us-east-1 required for CloudFront)
echo "Requesting ACM certificate..."
CERT_ARN=$("${AWS[@]}" acm list-certificates --region "$REGION" \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn" --output text)

if [ -z "$CERT_ARN" ] || [ "$CERT_ARN" = "None" ]; then
  CERT_ARN=$("${AWS[@]}" acm request-certificate \
    --domain-name "$DOMAIN" \
    --validation-method DNS \
    --region "$REGION" \
    --query "CertificateArn" --output text)
  echo "Certificate requested: $CERT_ARN"
fi

# Find the hosted zone for hipallo.com (needed for both cert validation and the alias)
ZONE_ID=$("${AWS[@]}" route53 list-hosted-zones \
  --query "HostedZones[?Name=='hipallo.com.'].Id" --output text | sed 's|/hostedzone/||')

# 6. Auto-add the DNS validation CNAME to Route 53, then wait for issuance
CERT_STATUS=$("${AWS[@]}" acm describe-certificate --certificate-arn "$CERT_ARN" \
  --region "$REGION" --query "Certificate.Status" --output text)

if [ "$CERT_STATUS" != "ISSUED" ]; then
  echo "Certificate status: $CERT_STATUS — adding DNS validation record..."
  # describe-certificate may take a moment to populate the validation record
  for i in $(seq 1 10); do
    VAL_NAME=$("${AWS[@]}" acm describe-certificate --certificate-arn "$CERT_ARN" \
      --region "$REGION" \
      --query "Certificate.DomainValidationOptions[0].ResourceRecord.Name" --output text)
    VAL_VALUE=$("${AWS[@]}" acm describe-certificate --certificate-arn "$CERT_ARN" \
      --region "$REGION" \
      --query "Certificate.DomainValidationOptions[0].ResourceRecord.Value" --output text)
    [ -n "$VAL_NAME" ] && [ "$VAL_NAME" != "None" ] && break
    sleep 3
  done

  "${AWS[@]}" route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
    --change-batch '{
      "Changes": [{
        "Action": "UPSERT",
        "ResourceRecordSet": {
          "Name": "'"$VAL_NAME"'",
          "Type": "CNAME",
          "TTL": 300,
          "ResourceRecords": [{"Value": "'"$VAL_VALUE"'"}]
        }
      }]
    }'
  echo "Validation record added. Waiting for certificate to be issued (this can take a few minutes)..."
  "${AWS[@]}" acm wait certificate-validated --certificate-arn "$CERT_ARN" --region "$REGION"
  echo "Certificate issued."
else
  echo "Certificate already issued: $CERT_ARN"
fi

# 7. Create or update CloudFront distribution
echo "Checking for existing CloudFront distribution..."
DIST_ID=$("${AWS[@]}" cloudfront list-distributions \
  --query "DistributionList.Items[?Aliases.Items[0]=='$DOMAIN'].Id" --output text)

if [ -z "$DIST_ID" ] || [ "$DIST_ID" = "None" ]; then
  echo "Creating CloudFront distribution..."
  DIST_ID=$("${AWS[@]}" cloudfront create-distribution \
    --distribution-config '{
      "CallerReference": "'"$DOMAIN-$(date +%s)"'",
      "Comment": "'"$DOMAIN"'",
      "Enabled": true,
      "DefaultRootObject": "index.html",
      "Origins": {
        "Quantity": 1,
        "Items": [{
          "Id": "S3-'"$BUCKET"'",
          "DomainName": "'"$BUCKET"'.s3-website-'"$REGION"'.amazonaws.com",
          "CustomOriginConfig": {
            "HTTPPort": 80,
            "HTTPSPort": 443,
            "OriginProtocolPolicy": "http-only"
          }
        }]
      },
      "DefaultCacheBehavior": {
        "TargetOriginId": "S3-'"$BUCKET"'",
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"], "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
        "ForwardedValues": {"QueryString": false, "Cookies": {"Forward": "none"}},
        "MinTTL": 0,
        "DefaultTTL": 300,
        "MaxTTL": 1200,
        "Compress": true
      },
      "Aliases": {
        "Quantity": 1,
        "Items": ["'"$DOMAIN"'"]
      },
      "ViewerCertificate": {
        "ACMCertificateArn": "'"$CERT_ARN"'",
        "SSLSupportMethod": "sni-only",
        "MinimumProtocolVersion": "TLSv1.2_2021"
      }
    }' --query "Distribution.Id" --output text)
  echo "Created distribution: $DIST_ID"
else
  echo "Distribution exists: $DIST_ID"
  invalidate_cf
fi

# Get CloudFront domain name
CF_DOMAIN=$("${AWS[@]}" cloudfront get-distribution --id "$DIST_ID" \
  --query "Distribution.DomainName" --output text)
echo "CloudFront domain: $CF_DOMAIN"

# 8. Create Route 53 alias record
echo "Creating Route 53 DNS record..."
if [ -n "$ZONE_ID" ] && [ "$ZONE_ID" != "None" ]; then
  "${AWS[@]}" route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
    --change-batch '{
      "Changes": [{
        "Action": "UPSERT",
        "ResourceRecordSet": {
          "Name": "'"$DOMAIN"'",
          "Type": "A",
          "AliasTarget": {
            "HostedZoneId": "Z2FDTNDATAQYW2",
            "DNSName": "'"$CF_DOMAIN"'",
            "EvaluateTargetHealth": false
          }
        }
      }]
    }'
  echo "DNS record created: $DOMAIN -> $CF_DOMAIN"
else
  echo "WARNING: Could not find hosted zone for hipallo.com"
  echo "Manually create an A record alias: $DOMAIN -> $CF_DOMAIN"
fi

echo ""
echo "=== Done! ==="
echo "Site will be live at https://$DOMAIN once CloudFront deploys (~5-10 min)"
