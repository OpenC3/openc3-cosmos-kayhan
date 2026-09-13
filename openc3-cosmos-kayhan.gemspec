# encoding: ascii-8bit

# Create the overall gemspec
Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-kayhan'
  s.summary = 'OpenC3 COSMOS Kayhan Space plugin'
  s.description = <<-EOF
    Sends spacecraft GPS telemetry from OpenC3 COSMOS to the Kayhan Space
    SatCat API for orbit determination
  EOF
  s.licenses = 'MIT'
  s.authors = ['Ryan Melton']
  s.email = ['plugins@openc3.com']
  s.homepage = 'https://github.com/OpenC3/openc3-cosmos-kayhan'
  s.platform = Gem::Platform::RUBY
  s.required_ruby_version = '>= 3.0'

  if ENV['VERSION']
    s.version = ENV['VERSION'].dup
  else
    time = Time.now.strftime("%Y%m%d%H%M%S")
    s.version = '0.0.0' + ".#{time}"
  end
  # Prefer pyproject.toml over requirements.txt
  python_dep_file = if File.exist?('pyproject.toml')
    'pyproject.toml'
  else
    'requirements.txt'
  end
  s.files = Dir.glob("{targets,lib,public,tools,microservices}/**/*").reject do |file|
    # Don't ship Python bytecode left behind by running the tests
    file.include?('__pycache__') || file.end_with?('.pyc')
  end + %w(Rakefile README.md LICENSE.md plugin.txt) + [python_dep_file]

  s.metadata = {
    # These fields are used when you submit your plugin to our App Store at store.openc3.com
    # See this help page for more detail: https://store.openc3.com/help/guidelines
    "source_code_uri" => "https://github.com/openc3/openc3-cosmos-kayhan",
    "openc3_store_title" => "Kayhan Space",
    "openc3_store_description" => "Periodically uploads spacecraft GPS history to the Kayhan Space SatCat API for orbit determination and reports the result as COSMOS telemetry.",
    "openc3_store_keywords" => "kayhan, satcat, gps, gnss, orbit determination, conjunction assessment, space traffic",
    "openc3_store_image" => "public/store_img.png",
    "openc3_cosmos_minimum_version" => "7.1.0",
    "openc3_store_access_type" => "public"
  }
end
