import * as events from "aws-cdk-lib/aws-events";
import * as kms from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";

export interface DocumentEventBrokerProps {
  readonly name: string;
  readonly kmsKey?: kms.IKey;
}

/**
 * Project-owned event boundary for terminal document notifications.
 *
 * Keeping this tiny construct under the historical `IDPBroker/EventBus`
 * construct path preserves the deployed EventBus logical id while removing
 * the application-level dependency on the catalog-blueprints broker.
 */
export class DocumentEventBroker extends Construct {
  readonly eventbus: events.EventBus;

  constructor(scope: Construct, id: string, props: DocumentEventBrokerProps) {
    super(scope, id);
    this.eventbus = new events.EventBus(this, "EventBus", {
      eventBusName: props.name,
      kmsKey: props.kmsKey,
    });
  }
}
