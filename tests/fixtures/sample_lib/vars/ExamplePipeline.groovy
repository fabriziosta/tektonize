import mylib.config.DeploymentType
import mylib.config.TestScenario

/**
 * Thin entry point for an example pipeline.
 */
def call(Map overrides = [:]) {
    def config = [
        deploymentType: DeploymentType.RPM,
        testScenario: TestScenario.FRESH_INSTALL,
    ]
    config.putAll(overrides)
    // Delegate to the main pipeline orchestrator
    return config
}
