import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { Construct } from 'constructs';

export interface SiteStackProps extends cdk.StackProps {
  /** The collector's raw snapshot bucket (read-only here). */
  rawBucket: s3.IBucket;
}

/**
 * The public Citi Bike Tides website.
 *
 *   CloudFront (HTTPS) ──> site bucket (private; only CloudFront can read it)
 *                            ├─ index.html, basemap.json, data/trips/*   uploaded by `cdk deploy` from site/
 *                            └─ data/live/*                              written by the builder
 *   EventBridge (every 5 min) ──> Builder Lambda: raw bucket ──> hourly files in data/live/
 *
 * Everything in the site bucket can be rebuilt (the page from git, live data from the raw bucket),
 * so unlike the raw bucket, this one is deleted with the stack.
 */
export class SiteStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SiteStackProps) {
    super(scope, id, props);

    const site = new s3.Bucket(this, 'Site', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, // public access goes through CloudFront only
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true, // empties the bucket so `cdk destroy` can remove it
    });

    const distribution = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'Citi Bike Tides',
      defaultRootObject: 'index.html',
      defaultBehavior: {
        // Origin Access Control: CloudFront signs its requests; the bucket policy admits only this distribution.
        origin: origins.S3BucketOrigin.withOriginAccessControl(site),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        compress: true, // gzip/brotli the JSON (~5x smaller over the wire)
        // Honors each file's Cache-Control: 60 s for live data, 5 min for the page and trip months.
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100, // North America + Europe edges: cheapest tier
    });

    // Upload site/ on every deploy. prune: false is essential: the default deletes anything in the
    // bucket that isn't in site/, which would wipe the builder's data/live/ files.
    new s3deploy.BucketDeployment(this, 'Deploy', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../../site'))],
      destinationBucket: site,
      prune: false,
      cacheControl: [s3deploy.CacheControl.fromString('public, max-age=300')],
      distribution,
      distributionPaths: ['/index.html', '/basemap.json', '/data/trips/*'],
    });

    const builder = new lambda.Function(this, 'Builder', {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../collector/builder')),
      memorySize: 1024, // more memory = more CPU for parsing ~60 snapshots of 1 MB JSON
      timeout: cdk.Duration.minutes(2),
      environment: { RAW_BUCKET: props.rawBucket.bucketName, SITE_BUCKET: site.bucketName },
      logGroup: new logs.LogGroup(this, 'BuilderLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    props.rawBucket.grantRead(builder, 'raw/*');      // read snapshots, never write or delete them
    site.grantReadWrite(builder, 'data/live/*');      // only its own folder of the site

    new events.Rule(this, 'BuildSchedule', {
      description: 'Pack new Citi Bike snapshots into hourly site files',
      // :02, :07, :12 ... so each run lands just after that minute's snapshot is saved
      schedule: events.Schedule.cron({ minute: '2/5' }),
      targets: [new targets.LambdaFunction(builder, { retryAttempts: 1 })],
    });

    new cdk.CfnOutput(this, 'SiteUrl', { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'BuilderName', { value: builder.functionName });
  }
}
